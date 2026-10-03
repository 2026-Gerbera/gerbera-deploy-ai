"""plan/intake: F1~F21 실패 대처를 로컬 file:// 저장소와 가짜 runner로 증명한다(네트워크 없음)."""

from __future__ import annotations

import base64
import errno
import fcntl
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.receive_deploy_request import ReceiveDeployRequestInput
from ddak.core.snapshots import digest_json, file_manifest
from ddak.executor.service import source_facts
from ddak.plan.intake import FetchPolicy, cleanup_stale_sources, receive_deploy_request
from ddak.plan.intake.fetch import GitResult, check_ref, check_url, fetch_repo, run_git
from ddak.plan.intake.logic import receive

DEPLOY = """\
tiers:
  web:
    paths: [web]
    dockerfile: web/Dockerfile
  was:
    paths: [app, requirements.txt]
    dockerfile: null
migrations_dir: migrations
env_example: .env.example
"""
GOOD: dict[str, str | bytes] = {
    "deploy.yaml": DEPLOY,
    "web/Dockerfile": "FROM nginx\n",
    "app/main.py": "print('hi')\n",
    "requirements.txt": "flask\n",
    "migrations/001.sql": "select 1;\n",
    ".env.example": "DB_HOST=\n",
}
GENV = {"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
CTX = RunContext("run1")


def git(repo: Path, *args: str) -> str:
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args]
    return subprocess.run(
        cmd, cwd=repo, env=GENV, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


def commit_files(
    repo: Path, files: Mapping[str, str | bytes], links: Mapping[str, str] | None = None
) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        git(repo, "init", "-q", "-b", "main")
    for name, body in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body if isinstance(body, bytes) else body.encode())
    for name, target in (links or {}).items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).unlink(missing_ok=True)
        (repo / name).symlink_to(target)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c", "--allow-empty")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "origin"
    commit_files(r, GOOD)
    return r


def policy(tmp_path: Path, **kw: Any) -> FetchPolicy:
    base: dict[str, Any] = {
        "allowed_schemes": ("file",),
        "allowed_hosts": None,
        "root": tmp_path / "src",
        "timeout_s": 20.0,
    }
    return FetchPolicy(**(base | kw))


def inp(repo_url: str, ref: str | None = None, run_id: str = "run1") -> ReceiveDeployRequestInput:
    req = DeployRequest(project="demo", repo_url=repo_url, ref=ref, target="local")
    return ReceiveDeployRequestInput(run_id=run_id, request=req)


def no_partials(root: Path) -> None:
    assert not list(root.rglob("*.partial")), list(root.rglob("*.partial"))


class Spy:
    """진짜 git을 부르되 호출을 기록한다. hook으로 호출 전에 상태를 바꿀 수 있다."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.before: Any = None

    def __call__(
        self, args: Sequence[str], env: Mapping[str, str], cwd: Path | None, timeout: float
    ) -> GitResult:
        self.calls.append((list(args), dict(env)))
        if self.before:
            self.before(args)
        return run_git(args, env, cwd, timeout)

    def count(self, sub: str) -> int:
        return sum(1 for a, _ in self.calls if sub in a)


def fake(results: list[GitResult | BaseException]) -> Any:
    """차례로 결과를 돌려주는 가짜 runner. 소진되면 마지막 값을 반복한다."""
    calls: list[tuple[list[str], dict[str, str]]] = []

    def runner(
        args: Sequence[str], env: Mapping[str, str], cwd: Path | None, timeout: float
    ) -> Any:
        calls.append((list(args), dict(env)))
        r = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(r, BaseException):
            raise r
        return r

    runner.calls = calls  # pyright: ignore[reportFunctionMemberAccess]
    return runner


def err(stderr: str, code: int = 128) -> GitResult:
    return GitResult(code, "", stderr)


def fetch(repo: Path, tmp_path: Path, **kw: Any) -> Any:
    pol: FetchPolicy = kw.pop("policy", None) or policy(tmp_path)
    return fetch_repo(f"file://{repo}", kw.pop("ref", None), run_id="run1", project="demo",
                      policy=pol, **kw)  # fmt: skip


def code_of(exc: pytest.ExceptionInfo[DdakToolError]) -> ErrorCode:
    return exc.value.code


# ---- T1 policy ---------------------------------------------------------------------------------


def test_policy_defaults_and_env() -> None:
    p = FetchPolicy.from_env({})
    assert (p.allowed_hosts, p.retries, p.cache_ttl_s, p.root) == (
        ("github.com",), 2, 3600.0, Path("var/sources"))  # fmt: skip
    p = FetchPolicy.from_env({
        "DDAK_GIT_ALLOWED_HOSTS": "GitHub.com, git.example.com", "DDAK_GIT_TIMEOUT_S": "5",
        "DDAK_GIT_MAX_MB": "1", "DDAK_GIT_MAX_FILES": "7", "DDAK_SOURCES_DIR": "x/y",
        "DDAK_GIT_CACHE_TTL_S": "10"})  # fmt: skip
    assert p.allowed_hosts == ("github.com", "git.example.com")
    assert (p.timeout_s, p.max_bytes, p.max_files, p.cache_ttl_s) == (5.0, 1048576, 7, 10.0)
    assert p.root == Path("x/y")


@pytest.mark.parametrize("key", ["DDAK_GIT_TIMEOUT_S", "DDAK_GIT_MAX_MB", "DDAK_GIT_MAX_FILES",
                                 "DDAK_GIT_CACHE_TTL_S"])  # fmt: skip
@pytest.mark.parametrize("bad", ["abc", "0", "-3"])
def test_policy_rejects_bad_numbers(key: str, bad: str) -> None:
    with pytest.raises(DdakToolError) as e:
        FetchPolicy.from_env({key: bad})
    assert code_of(e) is ErrorCode.CONFIG_INVALID


def test_policy_token_never_printed() -> None:
    secret = "ghp_" + "a" * 36
    p = FetchPolicy.from_env({"DDAK_GITHUB_TOKEN": secret})
    assert p.token is not None and p.token.get_secret_value() == secret
    assert secret not in repr(p) and secret not in str(p)


# ---- F1·F2 입력 검사 ---------------------------------------------------------------------------

BAD_URLS = [
    "http://github.com/o/r",
    "https://evil.example.com/o/r",
    "https://github.com/o",
    "https://github.com/o/r/extra",
    "https://github.com/o/r/",
    "https://github.com/o/../r",
    "https://user:pw@github.com/o/r",
    "https://user@github.com/o/r",
    "ssh://git@github.com/o/r",
    "git@github.com:o/r.git",
    "file:///tmp/x",
    "--upload-pack=touch /tmp/pwn",
    "https://github.com/o/r --upload-pack=x",
    "https://github.com/o/r\n--upload-pack=x",
    "https://github.com/o/r\n",
    "https://github.com:443/o/r",
    "ext::sh -c id",
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_f1_bad_url_rejected_before_git(url: str, tmp_path: Path) -> None:
    pol = FetchPolicy(root=tmp_path / "src")
    with pytest.raises(DdakToolError) as e:
        check_url(url, pol)
    assert code_of(e) is ErrorCode.CONFIG_INVALID
    runner = fake([err("x")])
    with pytest.raises(DdakToolError):
        fetch_repo(url, None, run_id="r", project="demo", policy=pol, runner=runner)
    assert runner.calls == []


def test_f1_good_urls_and_receive_rejects_without_fetcher_call(tmp_path: Path) -> None:
    pol = FetchPolicy(root=tmp_path / "src")
    assert check_url("https://github.com/o/r.git", pol) == "https://github.com/o/r.git"
    assert check_url("https://GitHub.com/o/r", pol)

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("fetcher must not be called")

    with pytest.raises(DdakToolError):
        receive(inp("http://github.com/o/r"), CTX, policy=pol, fetcher=boom)


def test_f1_credentials_rejected_by_model() -> None:
    with pytest.raises(ValidationError) as e:
        DeployRequest(project="demo", repo_url="https://u:pw@github.com/o/r", target="local")
    assert "pw" not in str(e.value)


@pytest.mark.parametrize("ref", ["-x", "--upload-pack=x", "a..b", "a b", "a;b", "a\nb", "", "x/",
                                 "a.lock", "~x", "a^b", "a:b", "*"])  # fmt: skip
def test_f2_bad_ref(ref: str) -> None:
    with pytest.raises(DdakToolError) as e:
        check_ref(ref)
    assert code_of(e) is ErrorCode.CONFIG_INVALID


def test_f2_dashdash_before_url_and_ref_ok(repo: Path, tmp_path: Path) -> None:
    spy = Spy()
    url = f"file://{repo}"
    fetch(repo, tmp_path, ref="main", runner=spy)
    net = [a for a, _ in spy.calls if url in a]
    assert net and all(a[a.index(url) - 1] == "--" for a in net)


# ---- F3 ---------------------------------------------------------------------------------------


def test_f3_git_missing(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    with pytest.raises(DdakToolError) as e:
        fetch(repo, tmp_path)
    assert code_of(e) is ErrorCode.ADAPTER_FAILED and "git이 필요" in e.value.message


# ---- F4·F5 재시도·타임아웃 ---------------------------------------------------------------------


def test_f4_retry_then_success(repo: Path, tmp_path: Path) -> None:
    spy, sleeps = Spy(), []
    n = {"fail": 2}

    real = spy.__call__

    def runner(
        args: Sequence[str], env: Mapping[str, str], cwd: Path | None, t: float
    ) -> GitResult:
        if "ls-remote" in args and n["fail"]:
            n["fail"] -= 1
            return err("fatal: unable to access: Could not resolve host: x")
        return real(args, env, cwd, t)

    got = fetch(repo, tmp_path, runner=runner, sleep=sleeps.append)
    assert len(got.commit) == 40 and sleeps == [1, 2]


def test_f4_retry_exhausted(tmp_path: Path) -> None:
    runner, sleeps = fake([err("fatal: Could not resolve host: github.com")]), []
    pol = FetchPolicy(root=tmp_path / "src", retries=2)
    with pytest.raises(DdakToolError) as e:
        fetch_repo("https://github.com/o/r", None, run_id="r", project="demo", policy=pol,
                   runner=runner, sleep=sleeps.append)  # fmt: skip
    assert code_of(e) is ErrorCode.ADAPTER_FAILED
    assert len(runner.calls) == 3 and sleeps == [1, 2]
    no_partials(pol.root)


def test_f5_timeout_cleans_partial(repo: Path, tmp_path: Path) -> None:
    real, pol = Spy(), policy(tmp_path)

    def runner(
        args: Sequence[str], env: Mapping[str, str], cwd: Path | None, t: float
    ) -> GitResult:
        if "fetch" in args:
            raise subprocess.TimeoutExpired(list(args), t)
        return real(args, env, cwd, t)

    with pytest.raises(DdakToolError) as e:
        fetch(repo, tmp_path, runner=runner, policy=pol)
    assert code_of(e) is ErrorCode.ADAPTER_TIMEOUT
    no_partials(pol.root)
    assert not (pol.root / "runs").exists() or not list((pol.root / "runs").rglob("run1"))


def test_f5_run_git_kills_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    # 수정11·12는 실제 kill 금지다. Popen/killpg 경로를 대역으로 검증하고 프로세스를 만들지 않는다.
    calls = []

    class Process:
        pid = 424242

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            if timeout is not None:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return b"", b""

    def popen(args, **kwargs):
        calls.append(("popen", args, kwargs["start_new_session"]))
        return Process()

    monkeypatch.setattr("ddak.plan.intake.fetch.subprocess.Popen", popen)
    monkeypatch.setattr(
        "ddak.plan.intake.fetch.os.killpg", lambda pid, sig: calls.append(("killpg", pid, sig))
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run_git(["fixture-git"], {}, None, 1.5)
    assert calls[0] == ("popen", ["fixture-git"], True)
    assert calls[1] == ("communicate", 1.5)
    assert calls[2] == ("killpg", 424242, signal.SIGKILL)
    assert calls[3] == ("communicate", None)


# ---- F6·F7·F18 오류 분류 ----------------------------------------------------------------------


@pytest.mark.parametrize("stderr", [
    "remote: Repository not found.\nfatal: repository 'https://github.com/o/r/' not found",
    "fatal: Authentication failed for 'https://github.com/o/r/'",
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    "fatal: unable to access 'https://github.com/o/r/': The requested URL returned error: 403",
])  # fmt: skip
def test_f6_auth_no_retry(stderr: str, tmp_path: Path) -> None:
    runner, sleeps = fake([err(stderr)]), []
    pol = FetchPolicy(root=tmp_path / "src")
    with pytest.raises(DdakToolError) as e:
        fetch_repo("https://github.com/o/r", None, run_id="r", project="demo", policy=pol,
                   runner=runner, sleep=sleeps.append)  # fmt: skip
    assert code_of(e) is ErrorCode.ADAPTER_FAILED
    assert "Git 머신 인증" in e.value.message and "존재하지 않는다" in e.value.message
    assert len(runner.calls) == 1 and sleeps == []


def test_f7_ref_not_found_real_repo(repo: Path, tmp_path: Path) -> None:
    spy = Spy()
    with pytest.raises(DdakToolError) as e:
        fetch(repo, tmp_path, ref="no-such-branch", runner=spy)
    assert code_of(e) is ErrorCode.ADAPTER_FAILED
    assert "ref" in e.value.message and str(repo) not in e.value.message
    assert spy.count("ls-remote") == 1 and spy.count("fetch") == 0


def test_f7_ref_error_from_git_no_retry(tmp_path: Path) -> None:
    runner = fake([err("fatal: couldn't find remote ref refs/heads/x")])
    pol = FetchPolicy(root=tmp_path / "src")
    with pytest.raises(DdakToolError) as e:
        fetch_repo("https://github.com/o/r", "a" * 40, run_id="r", project="demo", policy=pol,
                   runner=runner, sleep=lambda s: None)  # fmt: skip
    assert code_of(e) is ErrorCode.ADAPTER_FAILED and len(runner.calls) == 1


def test_f18_no_token_or_abs_path_in_message(tmp_path: Path) -> None:
    secret = "ghp_" + "b" * 36
    b64 = base64.b64encode(f"x-access-token:{secret}".encode()).decode()
    pol = FetchPolicy(root=tmp_path / "src", token=SecretStr(secret))
    junk = f"fatal: weird {secret} {b64} at /Users/someone/secret/dir {pol.root} " + "z" * 500
    runner = fake([err(junk)])
    with pytest.raises(DdakToolError) as e:
        fetch_repo("https://github.com/o/r", None, run_id="r", project="demo", policy=pol,
                   runner=runner)  # fmt: skip
    msg = str(e.value)
    assert secret not in msg and b64 not in msg and "/Users" not in msg and str(tmp_path) not in msg
    assert len(msg) < 260
    # 토큰은 argv·URL이 아니라 환경(extraheader)으로만 간다
    args, env = runner.calls[0]
    assert all(secret not in a and b64 not in a for a in args)
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert b64 in env["GIT_CONFIG_VALUE_0"]
    assert secret not in repr(pol)


# ---- F17 --------------------------------------------------------------------------------------


def test_f17_machine_env_and_hardening(repo: Path, tmp_path: Path) -> None:
    spy = Spy()
    fetch(repo, tmp_path, runner=spy)
    for args, env in spy.calls:
        assert env.get("HOME") == os.environ.get("HOME")
        assert env.get("GIT_CONFIG_GLOBAL") == os.environ.get("GIT_CONFIG_GLOBAL")
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ASKPASS"] == env["SSH_ASKPASS"] == "/usr/bin/false"
        assert env["GCM_INTERACTIVE"] == "never" and env["GH_PROMPT_DISABLED"] == "1"
        assert not any(k.startswith(("GIT_TRACE", "GCM_TRACE")) for k in env)
        assert f"core.hooksPath={os.devnull}" in args and "protocol.allow=never" in args
        assert "protocol.https.allow=always" in args
    assert "--no-recurse-submodules" in next(a for a, _ in spy.calls if "fetch" in a)


def test_f17_https_policy_does_not_allow_file_protocol(tmp_path: Path) -> None:
    runner = fake([err("x")])
    with pytest.raises(DdakToolError):
        fetch_repo("https://github.com/o/r", None, run_id="r", project="d",
                   policy=FetchPolicy(root=tmp_path / "s"), runner=runner)  # fmt: skip
    assert "protocol.file.allow=always" not in runner.calls[0][0]


# ---- 통합(T8) + F13·F16 -----------------------------------------------------------------------


def test_t8_integration_success(repo: Path, tmp_path: Path) -> None:
    pol = policy(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    out = receive(inp(f"file://{repo}", "main"), CTX, policy=pol)
    assert out.commit == head and len(out.commit) == 40
    src = pol.root / out.source_dir
    assert out.source_dir == "runs/demo/run1" and src.is_dir()
    manifest = file_manifest(src)
    assert out.snapshot.source_snapshot_hash == digest_json(manifest)
    assert out.snapshot.build_snapshot_hash == out.snapshot.source_snapshot_hash
    assert out.snapshot.patch_sha256 is None
    assert out.snapshot.source_snapshot_hash == source_facts(src)
    assert out.file_count == len(manifest) == len(GOOD) - 1  # .env*는 해시 제외
    assert out.ignored_symlinks == 0 and not (src / ".git").exists()
    assert out.deploy_config["tiers"]["was"]["dockerfile"] is None  # None은 정상
    assert out.deploy_config["migrations_dir"] == "migrations"
    assert str(tmp_path) not in out.model_dump_json().replace(out.repo_url, "")
    no_partials(pol.root)


@pytest.mark.parametrize("ref", [None, "main", "HEAD-tag", "SHA"])
def test_ref_kinds_resolve(repo: Path, tmp_path: Path, ref: str | None) -> None:
    git(repo, "tag", "HEAD-tag")
    head = git(repo, "rev-parse", "HEAD")
    got = fetch(repo, tmp_path, ref=head if ref == "SHA" else ref)
    assert got.commit == head
    assert (got.path / "deploy.yaml").exists()


def test_annotated_tag_resolves_to_commit(repo: Path, tmp_path: Path) -> None:
    git(repo, "tag", "-a", "v1", "-m", "v1")
    assert fetch(repo, tmp_path, ref="v1").commit == git(repo, "rev-parse", "HEAD")


def test_f13_commit_pinned_when_ref_moves(repo: Path, tmp_path: Path) -> None:
    first = git(repo, "rev-parse", "HEAD")
    spy = Spy()

    def push(args: Sequence[str]) -> None:
        if "fetch" in args and spy.before:
            spy.before = None
            commit_files(repo, {"app/new.py": "x = 1\n"})  # ls-remote 뒤, fetch 전에 ref 이동

    spy.before = push
    out = receive(inp(f"file://{repo}", "main"), CTX,
                  policy=policy(tmp_path), fetcher=partial(fetch_repo, runner=spy))  # fmt: skip
    assert out.commit == first and git(repo, "rev-parse", "HEAD") != first
    assert not (policy(tmp_path).root / out.source_dir / "app/new.py").exists()
    assert spy.count("ls-remote") == 1  # 이후 재조회 없음


def test_f16_hash_fixed_at_receive(repo: Path, tmp_path: Path) -> None:
    pol = policy(tmp_path)
    out = receive(inp(f"file://{repo}"), CTX, policy=pol)
    src = pol.root / out.source_dir
    (src / "app/main.py").write_text("evil\n")  # 접수 뒤 변조
    assert digest_json(file_manifest(src)) != out.snapshot.source_snapshot_hash


# ---- F8·F9 ------------------------------------------------------------------------------------


@pytest.mark.parametrize("kw", [{"max_bytes": 100}, {"max_files": 2}])
def test_f8_too_big(repo: Path, tmp_path: Path, kw: dict[str, int]) -> None:
    pol = policy(tmp_path, **kw)
    with pytest.raises(DdakToolError) as e:
        receive(inp(f"file://{repo}"), CTX, policy=pol)
    assert code_of(e) is ErrorCode.PRECONDITION_FAILED and "너무 크다" in e.value.message
    assert not (pol.root / "runs/demo/run1").exists()
    no_partials(pol.root)


def test_f9_submodule_rejected(tmp_path: Path) -> None:
    r = tmp_path / "o"
    commit_files(r, GOOD | {".gitmodules": '[submodule "x"]\n\tpath = x\n\turl = https://x/y\n'})
    pol = policy(tmp_path)
    with pytest.raises(DdakToolError) as e:
        receive(inp(f"file://{r}"), CTX, policy=pol)
    assert code_of(e) is ErrorCode.CONFIG_INVALID and "서브모듈" in e.value.message
    assert not (pol.root / "runs/demo/run1").exists()


def test_f9_lfs_pointer_rejected(tmp_path: Path) -> None:
    r = tmp_path / "o"
    ptr = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"0" * 64 + b"\nsize 9\n"
    commit_files(r, GOOD | {"model.bin": ptr})
    pol = policy(tmp_path)
    with pytest.raises(DdakToolError) as e:
        receive(inp(f"file://{r}"), CTX, policy=pol)
    assert code_of(e) is ErrorCode.CONFIG_INVALID and "LFS" in e.value.message
    assert not (pol.root / "runs/demo/run1").exists()


# ---- F10·F11·F12 ------------------------------------------------------------------------------


def test_f10_symlinks_ignored_not_failed(tmp_path: Path) -> None:
    r = tmp_path / "o"
    commit_files(r, GOOD, {"link.txt": "app/main.py", "app/dirlink": "../web", "dangling": "nope"})
    pol = policy(tmp_path)
    out = receive(inp(f"file://{r}"), CTX, policy=pol)
    src = pol.root / out.source_dir
    assert out.ignored_symlinks == 3
    assert not any(p.is_symlink() for p in src.rglob("*"))
    assert not (src / "link.txt").exists() and (src / "app/main.py").exists()
    assert out.snapshot.source_snapshot_hash == source_facts(src)
    cache = next((pol.root / "cache").glob("demo/*/*"))
    assert (cache / "link.txt").is_symlink()  # 캐시 원본은 그대로


def test_f10_symlink_log_lists_paths_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    r = tmp_path / "o"
    commit_files(r, GOOD, {f"l{i}": "app/main.py" for i in range(7)})
    pol = policy(tmp_path)
    receive(inp(f"file://{r}"), CTX, policy=pol)
    log = capsys.readouterr().err
    assert '"count": 7' in log and "l0" in log and "l6" not in log and str(tmp_path) not in log


def test_f10_hint_when_deploy_yaml_is_symlink(tmp_path: Path) -> None:
    r = tmp_path / "o"
    files = {k: v for k, v in GOOD.items() if k != "deploy.yaml"} | {"cfg/deploy.yaml": DEPLOY}
    commit_files(r, files, {"deploy.yaml": "cfg/deploy.yaml"})
    with pytest.raises(DdakToolError) as e:
        receive(inp(f"file://{r}"), CTX, policy=policy(tmp_path))
    assert code_of(e) is ErrorCode.CONFIG_INVALID and "링크" in e.value.message


def test_f12_hint_when_tier_path_is_symlink(tmp_path: Path) -> None:
    r = tmp_path / "o"
    files = {k: v for k, v in GOOD.items() if k != "web/Dockerfile"} | {"real/Dockerfile": "x"}
    commit_files(r, files, {"web": "real"})
    with pytest.raises(DdakToolError) as e:
        receive(inp(f"file://{r}"), CTX, policy=policy(tmp_path))
    assert "링크" in e.value.message


def run_bad(tmp_path: Path, files: dict[str, str | bytes]) -> DdakToolError:
    r = tmp_path / "o"
    commit_files(r, files)
    pol = policy(tmp_path)
    with pytest.raises(DdakToolError) as e:
        receive(inp(f"file://{r}"), CTX, policy=pol)
    assert code_of(e) is ErrorCode.CONFIG_INVALID
    assert not (pol.root / "runs/demo/run1").exists()  # 실패하면 run 사본 삭제
    no_partials(pol.root)
    return e.value


def test_f11_missing_yaml(tmp_path: Path) -> None:
    e = run_bad(tmp_path, {"a.txt": "x"})
    assert "deploy.yaml이 필요" in e.message and "deploy.example.yaml" in e.message


def test_f11_only_repo_root_yaml(tmp_path: Path) -> None:
    e = run_bad(tmp_path, {"sub/deploy.yaml": DEPLOY})
    assert "저장소 루트" in e.message


def test_f11_broken_yaml(tmp_path: Path) -> None:
    e = run_bad(tmp_path, GOOD | {"deploy.yaml": "tiers: [unclosed\n  - : :"})
    assert "해석" in e.message


def test_f11_not_a_mapping(tmp_path: Path) -> None:
    assert "매핑" in run_bad(tmp_path, GOOD | {"deploy.yaml": "- a\n- b\n"}).message


def test_f11_model_mismatch_shows_location_not_value(tmp_path: Path) -> None:
    bad = "tiers:\n  web:\n    paths: [web]\n    bogus: SECRETVALUE123\n"
    e = run_bad(tmp_path, GOOD | {"deploy.yaml": bad})
    assert "tiers.web.bogus" in e.message and "SECRETVALUE123" not in e.message


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "a/../../b"])
def test_f11_path_outside_repo(tmp_path: Path, path: str) -> None:
    bad = f"tiers:\n  web:\n    paths: [{path}]\n"
    e = run_bad(tmp_path, GOOD | {"deploy.yaml": bad})
    assert "tiers.web.paths" in e.message


@pytest.mark.parametrize(
    ("yaml_text", "needle"),
    [
        ("tiers:\n  web:\n    paths: [nope]\n", "tier web paths"),
        ("tiers:\n  web:\n    paths: [web]\n    dockerfile: web/Nope\n", "tier web dockerfile"),
        ("tiers:\n  web:\n    paths: [web]\nmigrations_dir: nomig\n", "migrations_dir"),
        ("tiers:\n  web:\n    paths: [web]\nenv_example: .env.nope\n", "env_example"),
    ],
)
def test_f12_missing_declared_paths(tmp_path: Path, yaml_text: str, needle: str) -> None:
    assert needle in run_bad(tmp_path, GOOD | {"deploy.yaml": yaml_text}).message


# ---- F14·F15 ----------------------------------------------------------------------------------


def test_f14_write_failure_leaves_nothing(repo: Path, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    def full(*a: Any, **k: Any) -> Any:
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(shutil, "copytree", full)
    pol = policy(tmp_path)
    with pytest.raises(DdakToolError) as e:
        fetch(repo, tmp_path, policy=pol)
    assert code_of(e) is ErrorCode.ADAPTER_FAILED and "ENOSPC" in e.value.message
    no_partials(pol.root)
    assert not (pol.root / "runs/demo/run1").exists()


def test_f14_download_failure_leaves_nothing(repo: Path, tmp_path: Path) -> None:
    pol, real = policy(tmp_path), Spy()

    def runner(
        args: Sequence[str], env: Mapping[str, str], cwd: Path | None, t: float
    ) -> GitResult:
        if "checkout" in args:
            return err("fatal: disk full")
        return real(args, env, cwd, t)

    with pytest.raises(DdakToolError):
        fetch(repo, tmp_path, runner=runner, policy=pol)
    no_partials(pol.root)
    assert not list((pol.root / "cache").glob("demo/*/*"))


def test_f15_duplicate_run_id(repo: Path, tmp_path: Path) -> None:
    pol = policy(tmp_path)
    out = receive(inp(f"file://{repo}"), CTX, policy=pol)
    marker = pol.root / out.source_dir / "marker"
    marker.write_text("keep")
    spy = Spy()
    with pytest.raises(DdakToolError) as e:
        receive(inp(f"file://{repo}"), CTX, policy=pol, fetcher=partial(fetch_repo, runner=spy))
    assert code_of(e) is ErrorCode.PRECONDITION_FAILED
    assert marker.read_text() == "keep" and spy.calls == []  # 덮어쓰지 않고 git도 안 부른다


# ---- 캐시 T6b: F13·F19~F21 --------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def rx(repo: Path, pol: FetchPolicy, run_id: str, spy: Spy, clock: Clock) -> Any:
    f = partial(fetch_repo, runner=spy, now=clock)
    return receive(inp(f"file://{repo}", "main", run_id), CTX, policy=pol, fetcher=f)


def test_cache_a_same_commit_within_ttl_no_clone(repo: Path, tmp_path: Path) -> None:
    pol, spy, clock = policy(tmp_path), Spy(), Clock()
    a = rx(repo, pol, "r1", spy, clock)
    assert spy.count("fetch") == 1
    clock.t += 3599
    b = rx(repo, pol, "r2", spy, clock)
    assert spy.count("fetch") == 1  # clone 0회 추가
    assert a.snapshot == b.snapshot and a.commit == b.commit


def test_cache_b_new_push_overwrites_immediately(repo: Path, tmp_path: Path) -> None:
    pol, spy, clock = policy(tmp_path), Spy(), Clock()
    a = rx(repo, pol, "r1", spy, clock)
    new = commit_files(repo, {"app/main.py": "print('v2')\n"})
    clock.t += 1
    b = rx(repo, pol, "r2", spy, clock)
    assert spy.count("fetch") == 2 and b.commit == new != a.commit
    assert b.snapshot != a.snapshot
    assert len(list((pol.root / "cache").glob("demo/*/*"))) == 1  # 덮어씀


def test_cache_c_ttl_expired_refetch(repo: Path, tmp_path: Path) -> None:
    pol, spy, clock = policy(tmp_path), Spy(), Clock()
    rx(repo, pol, "r1", spy, clock)
    clock.t += 3601
    rx(repo, pol, "r2", spy, clock)
    assert spy.count("fetch") == 2


@pytest.mark.parametrize("damage", ["no_git", "bad_head", "no_stamp"])
def test_cache_d_corrupt_cache_discarded_and_refetched(
    repo: Path, tmp_path: Path, damage: str
) -> None:
    pol, spy, clock = policy(tmp_path), Spy(), Clock()
    rx(repo, pol, "r1", spy, clock)
    cache = next((pol.root / "cache").glob("demo/*/*"))
    if damage == "no_git":
        shutil.rmtree(cache / ".git")
    elif damage == "bad_head":
        (cache / ".git/HEAD").write_text("0" * 40 + "\n")
    else:
        (cache / ".git/ddak-fetched").unlink()
    b = rx(repo, pol, "r2", spy, clock)  # 실패로 보고하지 않는다
    assert spy.count("fetch") == 2 and b.commit == git(repo, "rev-parse", "HEAD")


def test_f21_running_run_copy_survives_cache_overwrite(repo: Path, tmp_path: Path) -> None:
    pol, spy, clock = policy(tmp_path), Spy(), Clock()
    a = rx(repo, pol, "r1", spy, clock)
    src = pol.root / a.source_dir
    commit_files(repo, {"app/main.py": "print('v2')\n"})
    rx(repo, pol, "r2", spy, clock)  # 캐시를 새 푸시로 덮어씀
    assert digest_json(file_manifest(src)) == a.snapshot.source_snapshot_hash
    assert (src / "app/main.py").read_text() == "print('hi')\n"


def test_f20_concurrent_receive_clones_once(repo: Path, tmp_path: Path) -> None:
    pol, spy = policy(tmp_path), Spy()
    f = partial(fetch_repo, runner=spy)

    def go(i: int) -> Any:
        return receive(inp(f"file://{repo}", "main", f"c{i}"), CTX, policy=pol, fetcher=f)

    with ThreadPoolExecutor(4) as ex:
        outs = list(ex.map(go, range(4)))
    assert spy.count("fetch") == 1
    assert len({o.snapshot.source_snapshot_hash for o in outs}) == 1
    no_partials(pol.root)


def test_f20_lock_timeout(repo: Path, tmp_path: Path) -> None:
    pol = policy(tmp_path)
    receive(inp(f"file://{repo}", "main", "r1"), CTX, policy=pol)
    pol = replace(pol, timeout_s=0.3)
    lock = next((pol.root / "cache/demo").glob("*.lock"))
    with lock.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        with pytest.raises(DdakToolError) as e:
            receive(inp(f"file://{repo}", "main", "r2"), CTX, policy=pol)
    assert code_of(e) is ErrorCode.ADAPTER_TIMEOUT
    assert not (pol.root / "runs/demo/r2").exists()
    no_partials(pol.root)


# ---- T7 정리 ----------------------------------------------------------------------------------


def test_cleanup_stale_sources(repo: Path, tmp_path: Path) -> None:
    pol, spy, clock = policy(tmp_path), Spy(), Clock()
    clock.t = time.time()  # run 사본은 실제 mtime 기준
    rx(repo, pol, "old1", spy, clock)
    rx(repo, pol, "busy", spy, clock)
    leftover = pol.root / "runs/demo/crashed.partial"
    leftover.mkdir()
    now = clock.t
    # 아직 신선: 아무것도 안 지운다(.partial은 mtime 기준이라 신선하면 남는다)
    assert cleanup_stale_sources(pol.root, 3600, now=now) == []
    removed = cleanup_stale_sources(pol.root, 3600, keep={"busy"}, now=now + 7200)
    assert "runs/demo/old1" in removed and "runs/demo/crashed.partial" in removed
    assert any(r.startswith("cache/demo/") for r in removed)
    assert "runs/demo/busy" not in removed and (pol.root / "runs/demo/busy").is_dir()
    assert not (pol.root / "runs/demo/old1").exists()


# ---- T9 등록·공개 API -------------------------------------------------------------------------


def test_tool_registered() -> None:
    from ddak.app import load_tools

    assert "receive_deploy_request" in load_tools().registered()


def test_public_api_and_tool_wrapper(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ddak.plan.intake as pkg

    assert {"FetchPolicy", "cleanup_stale_sources", "receive_deploy_request"} <= set(pkg.__all__)
    monkeypatch.setenv("DDAK_SOURCES_DIR", str(tmp_path / "envsrc"))
    monkeypatch.setenv("DDAK_GIT_ALLOWED_HOSTS", "github.com")
    with pytest.raises(DdakToolError) as e:  # 환경 정책은 https/github.com만 허용한다
        receive_deploy_request(inp(f"file://{repo}"), CTX)
    assert code_of(e) is ErrorCode.CONFIG_INVALID
