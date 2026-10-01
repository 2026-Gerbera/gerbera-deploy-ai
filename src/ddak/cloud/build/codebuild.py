"""CodeBuild 호출(빌드 시작 → 완료 대기 → tier별 digest 읽기). 담당 C2. AI 없음.

- 소스(10/1 결정): 기본은 공개 GitHub 앱 저장소의 `ai-prod` 커밋 SHA(GitSource).
  브랜치 이름으로 빌드하지 않는다(승인 중 브랜치가 움직이면 승인 안 된 코드가 빌드된다).
  S3 zip(S3Source)은 대체 경로.
- 빌드 뒤 CodeBuild의 resolvedSourceVersion이 요청한 커밋(S3는 버전 ID)과 같은지 확인하고,
  실제로 빌드한 커밋 SHA를 BuildResult.revision으로 돌려준다(이미지 revision 라벨과 같은 값).
- 플랫폼 소유 buildspec만 쓴다. buildspecOverride는 보내지 않는다(배포 역할에서도 막는다).
- override env는 PLAINTEXT 값만 보낸다. buildspec 첫 줄이 같은 형식을 다시 검사한다
  (research/IAM E32: env 타입을 바꿔 push 토큰을 끌어오는 경로 차단).
- buildspec은 tier마다 index·amd64·arm64 digest를 exported variables로 내보낸다.
  이름은 exported_names()가 정한다(buildspec과 이 모듈이 같은 규칙을 쓴다).
- 실행기 취소는 스레드 안 boto3 호출을 멈추지 못하므로 deadline은 여기서 지킨다.
- 오류 메시지에 빌드 ARN·계정 ID·로그 원문을 넣지 않는다.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ddak.cloud.build.registries import PLATFORMS, check_digest
from ddak.core.contracts.base import TIER_PATTERN
from ddak.core.contracts.errors import DdakToolError, ErrorCode

# 💭 override 변수 이름(docs: TIERS / research/IAM 14-6: BUILD_TIERS·RELEASE_ID, 정준우와 확정 필요)
ENV_TIERS = "BUILD_TIERS"
ENV_RELEASE_ID = "RELEASE_ID"
ENV_REVISION = "SOURCE_REVISION"  # 이미지 revision 라벨 값(ai-prod 커밋 SHA)

_TIER = re.compile(TIER_PATTERN)
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
# 공개 저장소만 받는다. 자격증명이 섞인 URL(user:token@)·다른 호스트는 거부한다
_GITHUB_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_RUNNING = "IN_PROGRESS"
_DONE = "SUCCEEDED"
_PLATFORM_SUFFIX = {"linux/amd64": "AMD64", "linux/arm64": "ARM64"}


class CodeBuildClient(Protocol):
    def start_build(self, **kwargs: Any) -> dict[str, Any]: ...

    def batch_get_builds(self, **kwargs: Any) -> dict[str, Any]: ...

    def stop_build(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GitSource:
    """공개 GitHub 앱 저장소의 커밋(기본 경로). 커밋 SHA로 고정한다."""

    repository_url: str  # https://github.com/<owner>/<repo>
    commit_sha: str  # ai-prod 커밋(40자 hex)

    @property
    def revision(self) -> str:
        return self.commit_sha


@dataclass(frozen=True)
class S3Source:
    """S3에 올린 소스 zip 위치(대체 경로). 버전 ID로 고정해 승인 뒤 바뀐 소스를 빌드하지 않는다."""

    bucket: str
    key: str
    version_id: str
    revision: str  # zip을 만든 ai-prod 커밋(40자 hex). 이미지 라벨에 쓴다


BuildSource = GitSource | S3Source


@dataclass(frozen=True)
class TierDigests:
    index_digest: str
    platform_digests: Mapping[str, str]


@dataclass(frozen=True)
class BuildResult:
    build_id: str
    revision: str  # 실제로 빌드한 ai-prod 커밋 SHA
    digests: Mapping[str, TierDigests]  # tier -> digest 3개


def exported_names(tier: str) -> tuple[str, dict[str, str]]:
    """tier의 exported variable 이름: (index 이름, {플랫폼: 이름}). 예: was -> DDAK_WAS_INDEX."""
    prefix = f"DDAK_{_check_tier(tier).upper().replace('-', '_')}"
    return f"{prefix}_INDEX", {p: f"{prefix}_{_PLATFORM_SUFFIX[p]}" for p in PLATFORMS}


def start_build(
    client: CodeBuildClient,
    project: str,
    source: BuildSource,
    tiers: Sequence[str],
    release_id: str,
) -> str:
    """바뀐 tier만 빌드하도록 CodeBuild를 시작하고 build ID를 돌려준다."""
    if not tiers:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "빌드할 tier가 없다")
    if len(set(tiers)) != len(tiers):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "tier가 중복됐다")
    for tier in tiers:
        _check_tier(tier)
    if not _RELEASE_ID.fullmatch(release_id):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "release_id 형식이 아니다")
    source_args = _source_override(source)  # 형식 오류는 호출 전에 CONFIG_INVALID로
    try:
        response = client.start_build(
            projectName=project,
            **source_args,
            environmentVariablesOverride=[
                {"name": ENV_TIERS, "value": ",".join(tiers), "type": "PLAINTEXT"},
                {"name": ENV_RELEASE_ID, "value": release_id, "type": "PLAINTEXT"},
                {"name": ENV_REVISION, "value": source.revision, "type": "PLAINTEXT"},
            ],
        )
    except Exception as exc:  # botocore ClientError 등. 원문에는 ARN·계정 ID가 섞인다
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "CodeBuild 빌드를 시작하지 못했다") from exc
    return str(response["build"]["id"])


def wait_build(
    client: CodeBuildClient,
    build_id: str,
    deadline: float,
    *,
    poll_s: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """빌드가 끝날 때까지 읽기 폴링한다. deadline(monotonic)을 넘기면 빌드를 멈추고 실패한다."""
    while True:
        build = _describe(client, build_id)
        status = build.get("buildStatus")
        if status == _DONE:
            return build
        if status != _RUNNING:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, f"CodeBuild 빌드 실패: {status}")
        remaining = deadline - clock()
        if remaining <= 0:
            _stop_quietly(client, build_id)
            raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "CodeBuild 빌드가 제한 시간을 넘겼다")
        sleep(min(poll_s, remaining))


def read_digests(build: Mapping[str, Any], tiers: Sequence[str]) -> dict[str, TierDigests]:
    """빌드의 exported variables에서 tier별 index·플랫폼 digest를 읽는다."""
    exported = {
        v.get("name"): v.get("value") for v in build.get("exportedEnvironmentVariables") or []
    }
    result: dict[str, TierDigests] = {}
    for tier in tiers:
        index_name, platform_names = exported_names(tier)
        try:
            index = check_digest(str(exported.get(index_name, "")))
            platforms = {
                p: check_digest(str(exported.get(n, ""))) for p, n in platform_names.items()
            }
        except DdakToolError as exc:
            raise DdakToolError(
                ErrorCode.ADAPTER_FAILED, f"빌드 결과에 {tier} digest가 없거나 형식이 틀리다"
            ) from exc
        result[tier] = TierDigests(index_digest=index, platform_digests=platforms)
    return result


def run_build(
    client: CodeBuildClient,
    project: str,
    source: BuildSource,
    tiers: Sequence[str],
    release_id: str,
    deadline: float,
    *,
    poll_s: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> BuildResult:
    """start_build → wait_build → 소스 확인 → read_digests.

    build_image 툴(cloud 어댑터)이 부른다.
    """
    build_id = start_build(client, project, source, tiers, release_id)
    build = wait_build(client, build_id, deadline, poll_s=poll_s, clock=clock, sleep=sleep)
    check_built_source(build, source)
    return BuildResult(
        build_id=build_id, revision=source.revision, digests=read_digests(build, tiers)
    )


def check_built_source(build: Mapping[str, Any], source: BuildSource) -> None:
    """CodeBuild가 실제로 받은 소스 버전(resolvedSourceVersion)이 요청과 같은지 확인한다.

    GitHub는 커밋 SHA, S3는 객체 버전 ID다. 다르면 승인하지 않은 코드가 빌드됐을 수 있다.
    """
    expected = source.commit_sha if isinstance(source, GitSource) else source.version_id
    if build.get("resolvedSourceVersion") != expected:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "빌드한 소스 버전이 요청과 다르다")


def _source_override(source: BuildSource) -> dict[str, str]:
    if not _COMMIT_SHA.fullmatch(source.revision):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "커밋 SHA 형식이 아니다(40자 hex)")
    if isinstance(source, GitSource):
        if not _GITHUB_URL.fullmatch(source.repository_url):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "공개 GitHub 저장소 URL 형식이 아니다")
        return {
            "sourceTypeOverride": "GITHUB",
            "sourceLocationOverride": source.repository_url,
            "sourceVersion": source.commit_sha,
        }
    return {
        "sourceTypeOverride": "S3",
        "sourceLocationOverride": f"{source.bucket}/{source.key}",
        "sourceVersion": source.version_id,
    }


def _check_tier(tier: str) -> str:
    if not _TIER.fullmatch(tier):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "tier 이름 형식이 아니다")
    return tier


def _describe(client: CodeBuildClient, build_id: str) -> dict[str, Any]:
    try:
        builds = client.batch_get_builds(ids=[build_id]).get("builds") or []
    except Exception as exc:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "CodeBuild 상태를 읽지 못했다") from exc
    if not builds:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "CodeBuild 빌드를 찾지 못했다")
    return builds[0]


def _stop_quietly(client: CodeBuildClient, build_id: str) -> None:
    # 시간 초과 뒤 남은 빌드가 나중에 push하지 않게 멈춘다. 실패해도 시간 초과 오류가 우선이다.
    with contextlib.suppress(Exception):
        client.stop_build(id=build_id)


__all__ = [
    "ENV_RELEASE_ID",
    "ENV_REVISION",
    "ENV_TIERS",
    "BuildResult",
    "BuildSource",
    "CodeBuildClient",
    "GitSource",
    "S3Source",
    "TierDigests",
    "check_built_source",
    "exported_names",
    "read_digests",
    "run_build",
    "start_build",
    "wait_build",
]
