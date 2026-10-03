"""GitHub 저장소 가져오기(git 실행 전담). 실패 지점 F1~F7, F13, F14, F17~F21.

받은 코드는 실행하지 않고 데이터로만 다룬다. git은 최소 환경·리스트 인자·타임아웃으로만 실행한다.
"""

from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.git_credentials import credential_source, helper_options, isolated_git_env
from ddak.core.logging import get_logger
from ddak.core.redact import redact
from ddak.plan.intake.policy import FetchPolicy

__all__ = [
    "Checkout",
    "GitResult",
    "check_ref",
    "check_url",
    "fetch_repo",
    "fetched_at",
    "resolve_head",
    "run_git",
    "warm_cache",
]

_log = get_logger("plan")
_HTTPS = re.compile(r"https://(?P<host>[A-Za-z0-9.-]+)/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_FILE = re.compile(r"file:///[A-Za-z0-9_./+-]+")  # 테스트 전용(allowed_schemes에 file이 있을 때)
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_SHA = re.compile(r"[0-9a-f]{40}")
_ABS_PATH = re.compile(r"(?<![\w.:/])/(?:[\w.@+-]+/)+[\w.@+-]*")
FETCHED_FILE = "ddak-fetched"  # .git 안에 둔다(복사·해시에서 제외됨)


@dataclass(frozen=True)
class Checkout:
    path: Path  # run 전용 사본(불변)
    commit: str  # 40자 SHA


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], Mapping[str, str], Path | None, float], GitResult]


def run_git(
    args: Sequence[str], env: Mapping[str, str], cwd: Path | None, timeout: float
) -> GitResult:
    """git 실행. 타임아웃이면 프로세스 그룹까지 종료하고 TimeoutExpired를 올린다(F5)."""
    proc = subprocess.Popen(
        list(args),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return GitResult(proc.returncode, out.decode(errors="replace"), err.decode(errors="replace"))


# ---- F1·F2: git 실행 전 입력 검사 -------------------------------------------------------------


def check_url(url: str, policy: FetchPolicy) -> str:
    if "file" in policy.allowed_schemes and _FILE.fullmatch(url) and ".." not in url:
        return url
    m = _HTTPS.fullmatch(url.removesuffix(".git")) if "https" in policy.allowed_schemes else None
    if not m or url.endswith("/"):
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "repo_url은 https://github.com/<owner>/<repo>[.git] 형식이어야 한다",
        )
    host = m.group("host").lower()
    if policy.allowed_hosts is not None and host not in policy.allowed_hosts:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "허용되지 않은 저장소 호스트다")
    if any(part in (".", "..") for part in url.split("/")[3:]):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "repo_url 경로가 올바르지 않다")
    return url


def check_ref(ref: str | None) -> str | None:
    if ref is None:
        return None
    if not _REF.fullmatch(ref) or ".." in ref or ref.endswith(("/", ".lock")):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "ref 형식이 올바르지 않다")
    return ref


# ---- 환경·메시지 정리(F17·F18) ----------------------------------------------------------------


def _env(policy: FetchPolicy, url: str) -> dict[str, str]:
    options: list[str] = []
    if policy.credentials:
        if policy.credentials[2] != url:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "접수 인증 저장소가 다르다")
        source = credential_source(*policy.credentials)
        options = helper_options(*policy.credentials, source=source)
    else:
        source = "managed" if policy.token else "machine"
    # 머신 helper가 HOME/XDG/gh 설정을 평소대로 찾게 한다. 파일 내용은 Git만 읽는다.
    base = dict(os.environ) if source == "machine" else {}
    base["PATH"] = os.environ.get("PATH", os.defpath)
    env = isolated_git_env(base, source=source)
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    if not policy.credentials and policy.token and url.startswith("https://"):
        host = url.split("/")[2]
        cred = base64.b64encode(f"x-access-token:{policy.token.get_secret_value()}".encode())
        options = [f"http.https://{host}/.extraheader=AUTHORIZATION: basic {cred.decode()}"]
    if source == "machine" and not options:
        options = ["credential.interactive=false"]
    try:
        offset = int(env.get("GIT_CONFIG_COUNT", "0"))
        if offset < 0:
            raise ValueError
    except ValueError:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "Git 머신 설정 개수 형식 오류") from None
    env["GIT_CONFIG_COUNT"] = str(offset + len(options))
    for index, option in enumerate(options, offset):
        key, value = option.split("=", 1)
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _clean(text: str, policy: FetchPolicy) -> str:
    """git stderr를 메시지로 쓰기 전: 토큰·절대 경로 제거, 200자로 자른다."""
    if policy.credentials or not policy.token:
        # 머신/관리 helper의 값은 이 호출자에게 opaque라 문자열 치환으로 보호할 수 없다.
        return "원문 출력은 숨김"
    if policy.token:
        secret = policy.token.get_secret_value()
        for s in (secret, base64.b64encode(f"x-access-token:{secret}".encode()).decode()):
            text = text.replace(s, "[REDACTED]")
    text = text.replace(str(policy.root.resolve()), "<root>").replace(str(policy.root), "<root>")
    text = _ABS_PATH.sub("<path>", redact(" ".join(text.split())))
    return text[:200]


_AUTH = ("repository not found", "authentication failed", "could not read username",
         "terminal prompts disabled", "returned error: 403", "returned error: 404",
         "returned error: 401", "invalid credentials", "permission denied")  # fmt: skip
_NOREF = ("couldn't find remote ref", "not our ref", "unadvertised object", "invalid refspec",
          "no such remote ref")  # fmt: skip
_TRANSIENT = ("could not resolve host", "connection", "timed out", "reset by peer", "early eof",
              "returned error: 5", "remote end hung up", "tls", "unable to access",
              "temporary failure")  # fmt: skip


def _classify(stderr: str) -> str:
    low = stderr.lower()
    for kind, needles in (("auth", _AUTH), ("ref", _NOREF), ("transient", _TRANSIENT)):
        if any(n in low for n in needles):
            return kind
    return "other"


class _Git:
    """git 명령 실행기: 환경 최소화, 재시도(F4), 타임아웃(F5), 오류 분류(F6·F7)."""

    def __init__(
        self, policy: FetchPolicy, url: str, runner: Runner, sleep: Callable[[float], None]
    ):
        self.policy, self.url, self.runner, self.sleep = policy, url, runner, sleep
        self.env = _env(policy, url)
        git = shutil.which("git", path=self.env["PATH"])
        if not git:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "git이 필요하다(설치되어 있지 않다)")
        proto = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always"]
        if "file" in policy.allowed_schemes:
            proto += ["-c", "protocol.file.allow=always"]
        self.prefix = [git, "-c", f"core.hooksPath={os.devnull}", *proto]

    def run(self, args: Sequence[str], cwd: Path | None = None, *, network: bool = False) -> str:
        attempts = self.policy.retries + 1 if network else 1
        for i in range(attempts):
            try:
                res = self.runner([*self.prefix, *args], self.env, cwd, self.policy.timeout_s)
            except subprocess.TimeoutExpired:
                raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "git 실행 시간이 초과됐다") from None
            except OSError:
                raise DdakToolError(
                    ErrorCode.ADAPTER_FAILED, "git 실행 실패; 원문 출력은 숨김"
                ) from None
            if res.returncode == 0:
                return res.stdout
            kind = _classify(res.stderr)
            if kind == "auth":
                raise DdakToolError(
                    ErrorCode.ADAPTER_FAILED,
                    "저장소에 접근할 수 없거나 존재하지 않는다. "
                    "Git 머신 인증 또는 관리 저장소 토큰의 읽기 권한을 확인하세요",
                )
            if kind == "ref":
                raise DdakToolError(ErrorCode.ADAPTER_FAILED, "요청한 ref를 찾을 수 없다")
            if kind == "transient" and network and i + 1 < attempts:
                _log.warning("git 일시 오류, 재시도", attempt=i + 1)
                self.sleep(2**i)  # 1s, 2s
                continue
            msg = "네트워크 오류로 저장소를 받지 못했다" if kind == "transient" else "git 실행 실패"
            raise DdakToolError(
                ErrorCode.ADAPTER_FAILED, f"{msg}: {_clean(res.stderr, self.policy)}"
            )
        raise AssertionError("unreachable")  # pragma: no cover


# ---- 캐시·잠금·원자 이동(F13·F14·F19~F21) -----------------------------------------------------


def fetched_at(cache_dir: Path) -> float | None:
    try:
        return float((cache_dir / ".git" / FETCHED_FILE).read_text())
    except (OSError, ValueError):
        return None


def _rm(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


@contextmanager
def _locked(lock_path: Path, timeout_s: float) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as fh:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise DdakToolError(
                        ErrorCode.ADAPTER_TIMEOUT,
                        "같은 저장소 접수 잠금을 기다리다 시간이 초과됐다",
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _cache_ok(cache: Path, commit: str, now: float, ttl: float) -> bool:
    """F19: .git 없음·커밋 불일치·반쪽 캐시는 False. TTL은 받은 시각 기준."""
    try:
        head = (cache / ".git" / "HEAD").read_text().strip()
    except OSError:
        return False
    at = fetched_at(cache)
    return head == commit and at is not None and 0 <= now - at < ttl


def _resolve(git: _Git, ref: str | None) -> str:
    """F13: 커밋 SHA를 한 번 해석해 고정한다."""
    if ref and _SHA.fullmatch(ref):
        return ref
    names = (
        ["HEAD"]
        if ref is None
        else [f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}"]
    )
    if ref and ref.startswith("refs/"):
        names = [ref, f"{ref}^{{}}"]
    out = git.run(["ls-remote", "--", git.url, *names], network=True)
    found: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if _SHA.fullmatch(sha):
            found[name] = sha
    for name in names:  # 우선순위: 브랜치 > 태그(peeled) > 태그
        if name in found:
            return found[name]
    raise DdakToolError(ErrorCode.ADAPTER_FAILED, "요청한 ref를 찾을 수 없다")


def _download(git: _Git, commit: str, cache: Path, stamp: float) -> None:
    partial = cache.with_name(cache.name + ".partial")
    _rm(partial)
    partial.mkdir(parents=True)
    try:
        git.run(["init", "-q", "--template=", "."], partial)
        git.run(
            ["fetch", "-q", "--depth", "1", "--no-tags", "--no-recurse-submodules",
             "--", git.url, commit],
            partial,
            network=True,
        )  # fmt: skip
        git.run(["-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"], partial)
        if git.run(["rev-parse", "HEAD"], partial).strip() != commit:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "받은 커밋이 고정한 커밋과 다르다")
        (partial / ".git" / FETCHED_FILE).write_text(str(stamp))
        partial.rename(cache)  # 원자 이동(F14). 이전 캐시는 호출 전에 폐기됨(F21: run은 자기 사본)
    except OSError as e:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, f"소스 저장 실패({_errno(e)})") from None
    finally:
        _rm(partial)


def _errno(e: OSError) -> str:
    return errno.errorcode.get(e.errno or 0, "OSError")


def fetch_repo(
    repo_url: str,
    ref: str | None,
    *,
    run_id: str,
    project: str,
    policy: FetchPolicy,
    runner: Runner = run_git,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> Checkout:
    url = check_url(repo_url, policy)
    ref = check_ref(ref)
    run_dir = policy.root / "runs" / project / run_id
    if run_dir.exists():  # F15
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "이미 접수된 run_id다")
    git = _Git(policy, url, runner, sleep)  # F3, F17, F18
    commit = _resolve(git, ref)
    key = hashlib.sha256(url.encode()).hexdigest()
    ref_key = hashlib.sha256((ref or "HEAD").encode()).hexdigest()[:16]
    base = policy.root / "cache" / project
    cache = base / key / ref_key
    run_partial = run_dir.with_name(run_id + ".partial")
    try:
        with _locked(base / f"{key}-{ref_key}.lock", policy.timeout_s):  # F20
            if not _cache_ok(cache, commit, now(), policy.cache_ttl_s):
                _log.info("소스 캐시 갱신", project=project)
                cache.parent.mkdir(parents=True, exist_ok=True)
                _rm(cache)  # 손상·만료 캐시 폐기(F19)
                _download(git, commit, cache, now())
            run_dir.parent.mkdir(parents=True, exist_ok=True)
            _rm(run_partial)
            shutil.copytree(
                cache, run_partial, symlinks=True, ignore=shutil.ignore_patterns(".git")
            )
            run_partial.rename(run_dir)
    except OSError as e:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, f"소스 사본 저장 실패({_errno(e)})") from None
    finally:
        _rm(run_partial)
    return Checkout(run_dir, commit)


def resolve_head(
    repo_url: str,
    ref: str | None,
    *,
    policy: FetchPolicy,
    runner: Runner = run_git,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """감시용: URL·ref 검사(F1·F2) 후 ls-remote 한 번으로 커밋 SHA를 얻는다."""
    git = _Git(policy, check_url(repo_url, policy), runner, sleep)
    return _resolve(git, check_ref(ref))


def warm_cache(
    repo_url: str,
    ref: str | None,
    *,
    project: str,
    policy: FetchPolicy,
    runner: Runner = run_git,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """캐시만 미리 채운다(run 사본 없음). fetch_repo와 같은 캐시 키·잠금을 쓴다. 커밋 SHA를 반환."""
    url = check_url(repo_url, policy)
    ref = check_ref(ref)
    git = _Git(policy, url, runner, sleep)
    commit = _resolve(git, ref)
    key = hashlib.sha256(url.encode()).hexdigest()
    ref_key = hashlib.sha256((ref or "HEAD").encode()).hexdigest()[:16]
    base = policy.root / "cache" / project
    cache = base / key / ref_key
    with _locked(base / f"{key}-{ref_key}.lock", policy.timeout_s):
        if not _cache_ok(cache, commit, now(), policy.cache_ttl_s):
            cache.parent.mkdir(parents=True, exist_ok=True)
            _rm(cache)
            _download(git, commit, cache, now())
    return commit
