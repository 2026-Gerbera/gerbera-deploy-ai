"""승인 SHA 사본에서 플랫폼 buildspec을 실행한다. 인증값과 빌드 로그는 수집하지 않는다."""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Protocol

import yaml

from ddak.cloud.build.codebuild import (
    RELEASE_ID_PATTERN,
    docker_hub_repo,
    exported_names,
    read_digests,
)
from ddak.cloud.build.release import ReleaseBuild, _artifact
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.infra_outputs import IMAGE_REPOSITORY_PATTERN
from ddak.core.contracts.release import ReleaseArtifacts
from ddak.core.pem import UnsupportedPemError
from ddak.core.private_values import private_directory, read_private
from ddak.core.redact import redact
from ddak.core.snapshots import digest_json, excluded, file_manifest


class LocalBuildRunner(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input: str | None,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


_configured_runner: LocalBuildRunner | None = None
_configured_paths: dict[str, str] = {}
_META_PATH = '"/tmp/ddak-meta-$t.json"'
_GITHUB = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_REGISTRY_AUTH_FAILURE = re.compile(
    r"unauthorized|authentication required|insufficient_scope|"
    r"requested access to the resource is denied|authorization failed|"
    r"incorrect username or password|push access denied",
    re.IGNORECASE,
)


def configure_local_build(
    *,
    runner: LocalBuildRunner | None = None,
    config: Mapping[str, str] | None = None,
) -> None:
    """앱 조립의 runner·제품 경로 주입. 실행별 경로는 ctx.platform.local_build 우선."""
    global _configured_runner, _configured_paths
    paths = _local_paths(config)
    _configured_runner = runner
    _configured_paths = paths


def _image_repo(repository: str) -> str:
    if not re.fullmatch(IMAGE_REPOSITORY_PATTERN, repository):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "승인 이미지 저장소 공통 형식이 아니다")
    return docker_hub_repo(repository)


def _subprocess_runner(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    input: str | None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input, timeout=timeout)
    except BaseException:
        # 제품이 시작한 프로세스 그룹만 정리한다. docker/빌더 중지 명령은 실행하지 않는다.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _local_paths(config: Mapping[str, str] | None) -> dict[str, str]:
    if config is None:
        return {}
    if not isinstance(config, Mapping) or set(config) != {"docker_config", "builder", "tool_dir"}:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "제품 로컬 빌드 경로 형식 오류")
    if not all(isinstance(value, str) and value for value in config.values()) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", config["builder"]
    ):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "제품 로컬 빌드 경로 형식 오류")
    try:
        for key in ("docker_config", "tool_dir"):
            if not Path(config[key]).is_absolute():
                raise ValueError
            with private_directory(config[key]) as fd:
                if key == "docker_config":
                    metadata = json.loads(read_private(fd, "config.json"))
                    if (
                        not metadata.get("auths")
                        or metadata.get("credsStore")
                        or metadata.get("credHelpers")
                    ):
                        raise ValueError
    except (OSError, ValueError, TypeError, AttributeError):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "제품 로컬 빌드 경로 검사 실패") from None
    return dict(config)


def _environment(config: Mapping[str, str] | None = None) -> dict[str, str]:
    # 로그인은 Docker CLI가 기존 설정으로 처리한다. 비밀 환경값·BASH_ENV·셸 옵션은 상속하지 않는다.
    allowed = ("PATH", "HOME", "DOCKER_CONFIG", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CERT_PATH")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.setdefault("PATH", os.defpath)
    paths = _local_paths((_configured_paths or None) if config is None else config)
    if paths:
        env["DOCKER_CONFIG"] = paths["docker_config"]
        env["BUILDX_BUILDER"] = paths["builder"]
        env["PATH"] = paths["tool_dir"] + os.pathsep + env["PATH"]
    return env


def _invoke(
    runner: LocalBuildRunner,
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    deadline: float,
    input: str | None = None,
    limit: float | None = None,
    failure: str = "로컬 빌드 명령 실패; 원문 출력은 숨김",
    registry_auth_hint: bool = False,
) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "로컬 빌드 제한 시간 초과")
    try:
        result = runner(
            argv,
            cwd=cwd,
            env=env,
            input=input,
            timeout=min(remaining, limit) if limit is not None else remaining,
        )
    except subprocess.TimeoutExpired:
        raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "로컬 빌드 제한 시간 초과") from None
    except Exception:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, failure) from None
    if result.returncode:
        if registry_auth_hint and _REGISTRY_AUTH_FAILURE.search(result.stderr or ""):
            raise DdakToolError(
                ErrorCode.ADAPTER_FAILED,
                "빌드 중 레지스트리 인증·권한 실패: docker login 필요; "
                "해당 레지스트리 계정과 저장소 접근 권한을 확인하세요",
            )
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, failure)
    if time.monotonic() >= deadline:
        raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "로컬 빌드 제한 시간 초과")
    # stderr는 로그나 오류 메시지에 넣지 않는다. stdout도 알려진 메타데이터만 사용한다.
    return redact(result.stdout or "", max_len=16384)


def _preflight(
    repository: str, runner: LocalBuildRunner, cwd: Path, env: Mapping[str, str], deadline: float
) -> list[str]:
    _image_repo(repository)
    info = _invoke(
        runner,
        ["docker", "system", "info"],
        cwd=cwd,
        env=env,
        deadline=deadline,
        limit=20,
        failure="Docker 데몬 또는 로그인 메타데이터를 확인할 수 없다",
    )
    # --format 경로는 Docker CLI에서 UserName을 채우지 않는다. helper get을 요청하지 않는다.
    usernames = re.findall(r"^[ \t]*Username:[ \t]*([^\r\n]*)$", info, re.MULTILINE)
    usernames = [name.strip() for name in usernames]
    warnings: list[str] = []
    if len(usernames) != 1 or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", usernames[0]):
        warnings.append(
            "Docker Hub 로그인 확인 불가 — 빌드 push 단계에서 실패하면 docker login을 확인하세요"
        )
    elif usernames[0] != repository.split("/", 1)[0]:
        warnings.append(
            "Docker Hub 로그인 사용자와 이미지 저장소 namespace가 다릅니다 — "
            "조직 저장소일 수 있으므로 push 권한을 확인하세요"
        )
    for command in (["bash", "--version"], ["jq", "--version"]):
        _invoke(runner, command, cwd=cwd, env=env, deadline=deadline, limit=10)
    builder = _invoke(
        runner,
        ["docker", "buildx", "inspect"],
        cwd=cwd,
        env=env,
        deadline=deadline,
        limit=20,
        failure="기존 Buildx 빌더를 확인할 수 없다",
    )
    name = re.search(r"^Name:\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*$", builder, re.MULTILINE)
    driver = re.search(
        r"^Driver:\s*(docker|docker-container|kubernetes|remote)\s*$", builder, re.MULTILINE
    )
    platforms: set[str] = set()
    running = False
    for line in builder.splitlines():
        if line.strip().startswith("Name:"):
            running = False
        if re.fullmatch(r"Status:\s+running", line.strip()):
            running = True
        if running and line.strip().startswith("Platforms:"):
            # '*'는 실제 탐지가 아닌 수동 선언이므로 능력 증거로 쓰지 않는다.
            platforms.update(p.strip() for p in line.split(":", 1)[1].split(",") if "*" not in p)
    arm64 = any(p == "linux/arm64" or p.startswith("linux/arm64/v") for p in platforms)
    expected_builder = env.get("BUILDX_BUILDER")
    if (
        name is None
        or driver is None
        or "linux/amd64" not in platforms
        or not arm64
        or (expected_builder is not None and name.group(1) != expected_builder)
    ):
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED,
            "실행 중인 기존 Buildx 빌더의 linux/amd64·linux/arm64 지원을 확인할 수 없다",
        )
    # 빌드 도중 다른 프로세스가 기본 빌더를 바꾸더라도 검사한 빌더를 사용한다.
    if isinstance(env, dict):
        env["BUILDX_BUILDER"] = name.group(1)
    return warnings


def preflight_local_build(
    repository: str,
    *,
    runner: LocalBuildRunner | None = None,
    config: Mapping[str, str] | None = None,
) -> list[str]:
    """승인 전 검사. repository는 namespace/repository 공통 형식이다.

    Username은 기존 로그인 흔적이며 토큰 유효성·push 권한의 증명은 아니다.
    메타데이터 누락·중복·namespace 불일치는 경고로 반환한다. 빌더 검사는 차단한다.
    """
    with tempfile.TemporaryDirectory(prefix="ddak-local-preflight-") as temp:
        return _preflight(
            repository,
            runner or _configured_runner or _subprocess_runner,
            Path(temp),
            _environment(config),
            time.monotonic() + 60,
        )


def _script(tier: str) -> str:
    # CodeBuild 원문과 같은 패키지 리소스를 매번 읽는다. 별도 phase 복사본은 없다.
    try:
        spec = yaml.safe_load(files("ddak.cloud.build").joinpath("buildspec.yml").read_text())
        phases = spec["phases"]
        pre = phases["pre_build"]["commands"]
        build = phases["build"]["commands"]
        post = phases["post_build"]["commands"]
        index, platforms = exported_names(tier)
        names = [index, *platforms.values()]
        if not set(names) <= set(spec["env"]["exported-variables"]):
            raise ValueError
        # 생략 대상은 현재 플랫폼 buildspec의 정확한 4개 준비 명령뿐이다.
        if (
            spec.get("version") != 0.2
            or spec["env"].get("shell") != "bash"
            or set(phases) != {"pre_build", "build", "post_build"}
            or len(pre) != 5
            or len(build) != 1
            or len(post) != 1
            or pre[1]
            != 'echo "$DOCKERHUB_TOKEN" | docker login -u "$DOCKERHUB_USER" --password-stdin'
            or not re.fullmatch(
                r"docker run --privileged --rm tonistiigi/binfmt@sha256:[0-9a-f]{64} "
                r"--install arm64",
                pre[2],
            )
            or not re.fullmatch(
                r"docker buildx create --name ddak --driver docker-container --driver-opt "
                r"image=moby/buildkit@sha256:[0-9a-f]{64} --use",
                pre[3],
            )
            or pre[4] != "docker buildx inspect --bootstrap > /dev/null"
            or build[0].count(_META_PATH) != 1
            or post[0].count(_META_PATH) != 1
        ):
            raise ValueError
        commands = [pre[0], build[0], post[0]]
        # 고정 /tmp 파일 충돌만 바꾼다. 캐시·라벨·태그·플랫폼·digest 코드는 그대로다.
        commands[1:] = [
            c.replace(_META_PATH, '"$DDAK_LOCAL_META/ddak-meta-$t.json"') for c in commands[1:]
        ]
        if any(
            "DOCKERHUB_" in c or re.search(r"docker\s+(login|buildx\s+create)|tonistiigi/binfmt", c)
            for c in commands
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, yaml.YAMLError):
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, "플랫폼 buildspec의 로컬 변환 계약이 달라졌다"
        ) from None
    exports = "\n".join(f"printf '%s=%s\\n' '{name}' \"${{{name}}}\"" for name in names)
    return (
        "set -euo pipefail\n"
        "trap 'export CODEBUILD_BUILD_SUCCEEDING=0' ERR\n"
        # stderr는 subprocess 메모리에서 인증 오류 문구만 판별하고 저장·출력하지 않는다.
        "{\n" + "\n".join(commands) + "\n} > /dev/null\n" + exports + "\n"
    )


def _manifest(root: Path) -> dict:
    # 이름만 먼저 검사한다. .env·키·Docker 자격증명 파일 내용은 읽지 않는다.
    for parent, dirs, names in os.walk(root, followlinks=False):
        relative = Path(parent).relative_to(root)
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in [*dirs, *names]:
            path = relative / name
            if path.suffix.lower() == ".key":
                raise DdakToolError(ErrorCode.PRECONDITION_FAILED, str(UnsupportedPemError(path)))
            if (root / path).is_symlink():
                raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "빌드 소스에 심볼릭 링크가 있다")
            if any(
                p.lower() in {".docker", ".secrets", ".aws", ".ssh", ".claude"} for p in path.parts
            ) or (
                any(
                    p.lower().startswith(".env") and p not in {".env.example", ".env.sample"}
                    for p in path.parts
                )
            ):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "빌드 소스에 비밀 파일 경로가 있다"
                )
            if excluded(path) and path.name not in {".env.example", ".env.sample"}:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "빌드 소스에 승인 manifest 밖의 파일이 있다"
                )
    try:
        return file_manifest(root)
    except UnsupportedPemError as exc:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, str(exc)) from None
    except (OSError, ValueError):
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED, "빌드 소스 manifest를 확인할 수 없다"
        ) from None


def build_local_tier(tier: str, ctx: RunContext) -> ReleaseBuild:
    repository = ctx.image_repository
    if not isinstance(repository, str):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "승인된 로컬 이미지 저장소가 없다")
    approved_repository = repository
    repository = _image_repo(approved_repository)
    script = _script(tier)
    if ctx.source_binding is None or not ctx.repo_url or not ctx.candidate_sha:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "승인 스냅샷과 후보 Git SHA가 필요하다")
    fixture_url = (
        ctx.adapter_mode is AdapterMode.FAKE
        and _configured_runner is not None
        and ctx.repo_url.startswith("file:///")
    )
    if not (_GITHUB.fullmatch(ctx.repo_url) or fixture_url) or not re.fullmatch(
        r"[0-9a-f]{40}", ctx.candidate_sha
    ):
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, "공개 GitHub URL과 완전한 후보 SHA가 필요하다"
        )
    if not re.fullmatch(RELEASE_ID_PATTERN, ctx.run_id):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "release_id 형식이 아니다(run ID만)")
    current = ctx.release_artifacts
    if current is not None and current.snapshot != ctx.source_binding:
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED, "앞선 빌드 결과의 스냅샷이 이번 승인과 다르다"
        )
    runner = _configured_runner
    if ctx.adapter_mode is AdapterMode.FAKE and runner is None:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, "FAKE 로컬 빌드에는 configure_local_build runner가 필요하다"
        )
    runner = runner or _subprocess_runner
    deadline = ctx.deadline if ctx.deadline is not None else time.monotonic() + 900
    local_config = ctx.platform.get("local_build")
    if local_config is not None and not isinstance(local_config, Mapping):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "제품 로컬 빌드 경로 형식 오류")
    env = _environment(local_config)
    try:
        with tempfile.TemporaryDirectory(prefix="ddak-local-build-") as temp:
            root = Path(temp)
            checkout = root / "source"
            meta = root / "metadata"
            meta.mkdir()
            git_env = {
                **env,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            }
            git = [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "protocol.ext.allow=never",
                "-c",
                "http.followRedirects=false",
            ]
            _invoke(
                runner,
                [*git, "clone", "--no-checkout", "--template=", "--", ctx.repo_url, str(checkout)],
                cwd=root,
                env=git_env,
                deadline=deadline,
                limit=60,
            )
            _invoke(
                runner,
                [*git, "fetch", "--no-tags", "origin", ctx.candidate_sha],
                cwd=checkout,
                env=git_env,
                deadline=deadline,
                limit=60,
            )
            _invoke(
                runner,
                [*git, "checkout", "--detach", ctx.candidate_sha, "--"],
                cwd=checkout,
                env=git_env,
                deadline=deadline,
                limit=30,
            )
            actual = _invoke(
                runner,
                [*git, "rev-parse", "--verify", "HEAD^{commit}"],
                cwd=checkout,
                env=git_env,
                deadline=deadline,
                limit=10,
            ).strip()
            if actual != ctx.candidate_sha:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "checkout SHA와 승인 후보 SHA가 다르다"
                )
            manifest = _manifest(checkout)
            if digest_json(manifest) != ctx.source_binding.build_snapshot_hash:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED,
                    "후보 checkout의 build_files 해시가 승인과 다르다",
                )
            if ctx.build_source and _manifest(Path(ctx.build_source)) != manifest:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED,
                    "승인 빌드 사본과 후보 checkout의 build_files가 다르다",
                )
            _preflight(approved_repository, runner, root, env, deadline)
            if _manifest(checkout) != manifest:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "사전 검사 중 후보 checkout이 변경됐다"
                )
            env.update(
                {
                    "BUILD_TIERS": tier,
                    "RELEASE_ID": ctx.run_id,
                    "SOURCE_REVISION": ctx.candidate_sha,
                    "IMAGE_REPO": repository,
                    "CODEBUILD_BUILD_SUCCEEDING": "1",
                    "DDAK_LOCAL_META": str(meta),
                }
            )
            stdout = _invoke(
                runner,
                ["bash", "--noprofile", "--norc", "-s"],
                cwd=checkout,
                env=env,
                deadline=deadline,
                input=script,
                registry_auth_hint=True,
            )
            index, platforms = exported_names(tier)
            expected = {index, *platforms.values()}
            values: dict[str, str] = {}
            for line in stdout.splitlines():
                name, separator, value = line.partition("=")
                if not separator or name not in expected or name in values:
                    raise DdakToolError(
                        ErrorCode.ADAPTER_FAILED, "로컬 digest 출력 형식이 아니다; 원문 출력은 숨김"
                    )
                values[name] = value
            digests = read_digests(
                {
                    "exportedEnvironmentVariables": [
                        {"name": n, "value": v} for n, v in values.items()
                    ]
                },
                [tier],
            )[tier]
            images = dict(current.images) if current else {}
            images[tier] = _artifact(repository, digests.index_digest, digests.platform_digests)
            artifacts = ReleaseArtifacts(snapshot=ctx.source_binding, images=images)
            return ReleaseBuild(artifacts, "local:" + ctx.run_id + ":" + tier, actual)
    except OSError:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "로컬 빌드 임시 사본 처리 실패") from None


__all__ = ["LocalBuildRunner", "configure_local_build", "preflight_local_build"]
