"""첫 실행 Git 인증: Vault·Git·helper는 모두 stub, 외부 프로세스와 비밀 파일 접근 없음."""

from __future__ import annotations

import json
import os
import subprocess

import pytest
from pydantic import SecretStr

from ddak.core import candidate, git_credentials
from ddak.core.app_repository import AppRepository
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.plan.intake.fetch import GitResult, _env, resolve_head
from ddak.plan.intake.policy import FetchPolicy

URL = "https://github.com/fixture/app.git"
PROD = "a" * 40
AI_PROD = "b" * 40
OPAQUE = "fixture-" + "opaque-secret-never-print"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    state = {"raw": None, "reads": []}

    class Vault:
        def __init__(self, root):
            self.root = root

        def get(self, project, key):
            state["reads"].append((self.root, project, key))
            return state["raw"]

    def forbidden(*args, **kwargs):
        pytest.fail("실제 프로세스·kill 호출 금지")

    monkeypatch.setattr(git_credentials, "SecretVault", Vault)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr("ddak.plan.intake.fetch.shutil.which", lambda *a, **k: "/stub/git")
    return state


def managed(state):
    state["raw"] = json.dumps({"url": URL, "token": OPAQUE})


def repository(tmp_path, **kwargs):
    return AppRepository(tmp_path, credentials=(tmp_path / "vault", "demo", URL), **kwargs)


def test_absent_token_uses_machine_without_writes(tmp_path, isolated):
    args = (tmp_path / "vault", "demo", URL)
    assert git_credentials.credential_source(*args) == "machine"
    assert repository(tmp_path).credential_source == "machine"
    assert git_credentials.helper_options(*args) == ["credential.interactive=false"]
    assert list(tmp_path.iterdir()) == []
    assert isolated["reads"] and all(r == (*args[:2], "git_push_token") for r in isolated["reads"])


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{",
        "null",
        "[]",
        "{}",
        json.dumps({"url": 1, "token": OPAQUE}),
        json.dumps({"url": URL.replace("app.git", "other.git"), "token": OPAQUE}),
        json.dumps({"url": "https://u:p@github.com/fixture/app.git", "token": OPAQUE}),
        *[
            json.dumps({"url": URL, "token": t})
            for t in (None, 42, "", "x\ny", "x\x00", "x\x7f", "x" * 4097)
        ],
    ],
)
def test_malformed_managed_token_never_falls_back(tmp_path, isolated, raw):
    isolated["raw"] = raw
    for select in (
        lambda: git_credentials.credential_source(tmp_path / "vault", "demo", URL),
        lambda: git_credentials.helper_options(tmp_path / "vault", "demo", URL),
        lambda: repository(tmp_path),
        lambda: _env(FetchPolicy(credentials=(tmp_path / "vault", "demo", URL)), URL),
    ):
        with pytest.raises(DdakToolError, match="관리 push 토큰") as error:
            select()
        assert error.value.code is ErrorCode.PRECONDITION_FAILED
        assert OPAQUE not in str(error.value)


def test_managed_priority_and_connection_snapshot(tmp_path, isolated, monkeypatch):
    repo = repository(tmp_path)
    managed(isolated)
    assert repo.credential_source == "machine"
    connected = repository(tmp_path)
    assert connected.credential_source == "managed"
    options = git_credentials.helper_options(*connected.credentials)
    assert options[:1] == ["credential.helper="]
    assert "ddak.core.git_credentials" in options[1]
    assert OPAQUE not in repr(options)
    isolated["raw"] = None
    calls = []
    stub_process(monkeypatch, calls)
    connected.git("ls-remote", "origin", "refs/heads/prod")
    assert "credential.helper=" in calls[0][0]
    assert connected.credential_source == "managed"
    assert repository(tmp_path).credential_source == "machine"


@pytest.mark.parametrize("source", ["managed", "machine"])
def test_environment_keeps_machine_helpers_but_disables_prompts_and_traces(source):
    original = {
        "HOME": "/fixture/home",
        "XDG_CONFIG_HOME": "/fixture/xdg",
        "GH_CONFIG_DIR": "/fixture/gh",
        "GIT_CONFIG_GLOBAL": "/fixture/gitconfig",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "!gh auth git-credential",
        "GIT_TRACE": "1",
        "GIT_TRACE_CURL": "1",
        "GCM_TRACE": "1",
        "GIT_CURL_VERBOSE": "1",
        "GH_DEBUG": "api",
        "GIT_TERMINAL_PROMPT": "1",
        "GIT_ASKPASS": "interactive",
        "SSH_ASKPASS": "interactive",
    }
    env = git_credentials.isolated_git_env(original, source=source)
    assert original["GIT_TERMINAL_PROMPT"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == env["SSH_ASKPASS"] == "/usr/bin/false"
    assert env["GCM_INTERACTIVE"] == "never" and env["GH_PROMPT_DISABLED"] == "1"
    assert not any(k.startswith(("GIT_TRACE", "GCM_TRACE")) for k in env)
    assert "GIT_CURL_VERBOSE" not in env and "GH_DEBUG" not in env
    if source == "machine":
        for key in (
            "HOME",
            "XDG_CONFIG_HOME",
            "GH_CONFIG_DIR",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_VALUE_0",
        ):
            assert env[key] == original[key]
        assert "GIT_CONFIG_NOSYSTEM" not in env
    else:
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert "GIT_CONFIG_COUNT" not in env


def stub_process(monkeypatch, calls, *, returncode=0):
    class Process:
        def __init__(self):
            self.returncode = returncode

        def communicate(self, **kwargs):
            return f"{PROD}\trefs/heads/prod\n".encode(), OPAQUE.encode()

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)


@pytest.mark.parametrize("source", ["managed", "machine"])
@pytest.mark.parametrize("returncode", [0, 128])
def test_repository_commands_use_selected_mode_and_hide_stderr(
    tmp_path, isolated, monkeypatch, capsys, source, returncode
):
    if source == "managed":
        managed(isolated)
    calls = []
    stub_process(monkeypatch, calls, returncode=returncode)
    monkeypatch.setenv("HOME", "/fixture/home")
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    repo = repository(tmp_path)
    if returncode:
        with pytest.raises(DdakToolError) as error:
            repo.git("ls-remote", "origin", "refs/heads/prod")
        assert OPAQUE not in str(error.value)
    else:
        assert repo.git("ls-remote", "origin", "refs/heads/prod").startswith(PROD)
    argv, kw = calls[0]
    assert ("credential.helper=" in argv) == (source == "managed")
    assert (kw["env"].get("GIT_CONFIG_GLOBAL") == os.devnull) == (source == "managed")
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0" and kw["stdin"] == subprocess.DEVNULL
    assert OPAQUE not in repr(calls) + repr(repo.timings) + repr(capsys.readouterr())
    assert "config" not in argv and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "source,with_vault", [("managed", True), ("machine", True), ("machine", False)]
)
@pytest.mark.parametrize("failure", [None, "auth", "network", "opaque"])
def test_intake_uses_helpers_and_hides_opaque_errors(
    tmp_path, isolated, monkeypatch, capsys, source, failure, with_vault
):
    if source == "managed":
        managed(isolated)
    monkeypatch.setenv("HOME", "/fixture/home")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/fixture/xdg")
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    calls, sleeps = [], []
    errors = {"auth": "authentication failed", "network": "connection reset", "opaque": "fatal"}

    def runner(argv, env, cwd, timeout):
        calls.append((argv, env))
        if failure:
            return GitResult(128, "", errors[failure] + " " + OPAQUE)
        return GitResult(0, PROD + "\trefs/heads/prod\n", "")

    policy = FetchPolicy(
        root=tmp_path,
        credentials=(tmp_path / "vault", "demo", URL) if with_vault else None,
        # Vault 인증 경로는 과거 환경 토큰보다 우선한다.
        token=SecretStr("legacy-" + "fixture") if with_vault else None,
        retries=1,
    )
    if failure:
        with pytest.raises(DdakToolError) as error:
            resolve_head(URL, "prod", policy=policy, runner=runner, sleep=sleeps.append)
        assert OPAQUE not in str(error.value)
        assert len(calls) == (2 if failure == "network" else 1)
    else:
        assert resolve_head(URL, "prod", policy=policy, runner=runner) == PROD
    env = calls[0][1]
    assert (env.get("GIT_CONFIG_GLOBAL") == os.devnull) == (source == "managed")
    if source == "managed":
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_0"] == ""
    else:
        assert env["HOME"] == "/fixture/home" and env["XDG_CONFIG_HOME"] == "/fixture/xdg"
        assert "credential.helper" not in env.values()
    assert OPAQUE not in repr(calls) + repr(capsys.readouterr())
    assert list(tmp_path.iterdir()) == []


def test_intake_preserves_inherited_machine_helper_options(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!gh auth git-credential")
    env = _env(FetchPolicy(root=tmp_path), URL)
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_VALUE_0"] == "!gh auth git-credential"
    assert env["GIT_CONFIG_KEY_1"] == "credential.interactive"
    assert env["GIT_CONFIG_VALUE_1"] == "false"


@pytest.mark.parametrize("ai_exists", [False, True])
@pytest.mark.parametrize("branch", ["prod", "release", "refs/heads/release"])
def test_push_check_fetches_existing_ai_tip_before_plain_dry_run(
    tmp_path, monkeypatch, ai_exists, branch
):
    repo = repository(tmp_path)
    calls = []
    source_ref = "refs/heads/" + branch.removeprefix("refs/heads/")

    def git(*args, **kwargs):
        calls.append(args)
        if args[0] == "ls-remote":
            return (
                PROD
                + "\t"
                + source_ref
                + "\n"
                + (AI_PROD + "\trefs/heads/ai-prod\n" if ai_exists else "")
            )
        return ""

    monkeypatch.setattr(repo, "git", git)
    assert repo.check_push_access(branch) is None
    chosen = AI_PROD if ai_exists else PROD
    assert calls[-2:] == [
        ("fetch", "--no-tags", "origin", chosen),
        ("push", "--dry-run", "--porcelain", "origin", chosen + ":refs/heads/ai-prod"),
    ]
    assert not any("--force" in arg or arg == "config" for call in calls for arg in call)


@pytest.mark.parametrize("operation", ["ls-remote", "fetch", "push"])
def test_preflight_push_failure_blocks_source_scan_with_safe_precondition(
    tmp_path, monkeypatch, operation
):
    repo = repository(tmp_path)
    calls = []

    def git(*args, **kwargs):
        calls.append(args)
        if args[0] == operation:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, OPAQUE)
        return PROD + "\trefs/heads/prod\n" if args[0] == "ls-remote" else ""

    def never_scan(*args, **kwargs):
        pytest.fail("push 점검 실패 뒤 소스 검사 실행")

    monkeypatch.setattr(repo, "git", git)
    monkeypatch.setattr(candidate, "preflight_source", never_scan)
    with pytest.raises(DdakToolError, match=r"설정 필요: .*push") as error:
        repo.preflight_source(PROD)
    assert error.value.code is ErrorCode.PRECONDITION_FAILED
    assert OPAQUE not in str(error.value)
    assert calls[-1][0] == operation


@pytest.mark.parametrize("source", ["managed", "machine"])
def test_result_summaries_record_connection_source(tmp_path, isolated, monkeypatch, source):
    if source == "managed":
        managed(isolated)
    repo = repository(tmp_path)
    monkeypatch.setattr(
        repo, "git", lambda *a, **k: PROD + "\trefs/heads/prod\n" if a[0] == "ls-remote" else ""
    )
    monkeypatch.setattr(candidate, "preflight_source", lambda *a, **k: {"source": "fixture"})
    monkeypatch.setattr(candidate, "prepare_candidate", lambda *a: {"candidate_sha": PROD})
    # 재연결 전까지 토큰 추가·삭제로 기존 연결의 출처를 바꾸지 않는다.
    isolated["raw"] = None if source == "managed" else json.dumps({"url": URL, "token": OPAQUE})
    for result in (
        repo.preflight_source(PROD),
        repo.prepare_candidate(),
        repo.publish(PROD, {"local"}, set()),
        repo.publish(PROD, {"local"}, {"local"}),
    ):
        assert result["git_auth_source"] == source
        assert OPAQUE not in repr(result)


def test_identity_config_is_read_only(tmp_path, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[:4] == ["git", "config", "--global", "--get"]
        return subprocess.CompletedProcess(
            argv, 0, "Operator" if argv[-1] == "user.name" else "op@example.test", ""
        )

    monkeypatch.setattr(subprocess, "run", run)
    assert git_credentials.configured_identity({}, tmp_path) == ("Operator", "op@example.test")
    assert len(calls) == 2 and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("source", ["managed", "machine"])
@pytest.mark.parametrize("code", [ErrorCode.ADAPTER_FAILED, ErrorCode.ADAPTER_TIMEOUT])
def test_connect_failure_is_safe_setup_precondition(tmp_path, isolated, monkeypatch, source, code):
    if source == "managed":
        managed(isolated)
    calls = []

    def git(self, *args, **kwargs):
        calls.append(args)
        if args[0] == "clone":
            raise DdakToolError(code, OPAQUE)
        assert args == ("ls-remote", "--get-url", URL)
        return URL

    monkeypatch.setattr(AppRepository, "git", git)
    with pytest.raises(DdakToolError, match=r"설정 필요: .*Git 인증") as error:
        AppRepository.connect(
            tmp_path / "checkout", URL, credentials=(tmp_path / "vault", "demo", URL)
        )
    assert error.value.code is ErrorCode.PRECONDITION_FAILED
    assert OPAQUE not in str(error.value)
    assert len(calls) == 2


@pytest.mark.parametrize("source", ["managed", "machine"])
def test_origin_auth_header_check_reads_config_without_writes(
    tmp_path, isolated, monkeypatch, source
):
    if source == "managed":
        managed(isolated)
    repo = repository(tmp_path)
    calls = []

    def git(*args, **kwargs):
        calls.append(args)
        if args[0] == "config":
            assert args == (
                "config",
                "--includes",
                "--name-only",
                "--get-regexp",
                r"^http\..*extraheader$",
            )
            return ""
        assert args in (
            ("remote", "get-url", "--all", "origin"),
            ("remote", "get-url", "--push", "--all", "origin"),
        )
        return URL

    monkeypatch.setattr(repo, "git", git)
    repo.require_origin(URL)
    assert len(calls) == (3 if source == "managed" else 2)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("output", ["", "invalid-output", "bad-sha\trefs/heads/prod"])
def test_invalid_remote_listing_is_safe_push_precondition(tmp_path, monkeypatch, output):
    repo = repository(tmp_path)
    calls = []

    def git(*args, **kwargs):
        calls.append(args)
        return output if args[0] == "ls-remote" else ""

    monkeypatch.setattr(repo, "git", git)
    with pytest.raises(DdakToolError, match=r"설정 필요: .*push") as error:
        repo.check_push_access()
    assert error.value.code is ErrorCode.PRECONDITION_FAILED
    assert calls[-1][0] == "ls-remote"


def test_git_start_failure_hides_opaque_exception(tmp_path, monkeypatch):
    def popen(*args, **kwargs):
        raise OSError(OPAQUE)

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(DdakToolError) as error:
        repository(tmp_path).git("ls-remote", "origin", "refs/heads/prod")
    assert OPAQUE not in str(error.value)


def test_bad_inherited_config_count_hides_value(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", OPAQUE)
    with pytest.raises(DdakToolError) as error:
        _env(FetchPolicy(root=tmp_path), URL)
    assert OPAQUE not in str(error.value)
