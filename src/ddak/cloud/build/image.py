"""build_image 툴 본체: RunContext → build_tier(CodeBuild 1회) → BuildImageOutput. 담당 C2. AI 없음.

- 소스: GitSource(ctx.repo_url, ctx.candidate_sha). repo_url은 바꾸지 않는다(StartBuild IAM이
  source.location == repo_url 정확히 일치를 요구한다).
- release_id = ctx.run_id, 스냅샷 = ctx.source_binding(승인 뒤 실행기만 주입),
  앞선 결과 = ctx.release_artifacts.
- 인프라 출력: ctx.platform['cloud']의 codebuild_project_name,
  image_repository(`<네임스페이스>/<저장소>`).
- REAL은 호출마다 새 boto3 Session(AWS_PROFILE·AWS_REGION, 서울 기본). FAKE는 FakeCodeBuild이고
  AWS를 부르지 않는다. FAKE는 저장소 URL을 쓰지 않고(고정 가짜 URL), 커밋은 ctx.candidate_sha를
  쓰되 40자가 아니면 가짜 값으로, 빠진 인프라 출력도 결정적 가짜 값으로 채운다.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Mapping
from typing import Any

import boto3
from botocore.config import Config

from ddak.cloud.build.codebuild import CodeBuildClient, GitSource, docker_hub_repo
from ddak.cloud.build.fake import FakeCodeBuild
from ddak.cloud.build.local import build_local_tier
from ddak.cloud.build.release import build_tier
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.build_image import BuildImageOutput

DEFAULT_REGION = "ap-northeast-2"
DEFAULT_BUDGET_S = 900.0  # 카탈로그 build_image 제한과 같다. ctx.deadline이 없을 때만
FAKE_REPO_URL = "https://github.com/ddak-fake/app"
FAKE_PROJECT = "ddak-fake-build"
FAKE_IMAGE_REPOSITORY = "ddak-fake/app"


def build_image(tier: str, ctx: RunContext) -> BuildImageOutput:
    """tier를 빌드해 같은 run의 앞선 빌드 결과와 합친 릴리스 산출물을 돌려준다."""
    fake = ctx.adapter_mode is AdapterMode.FAKE
    if ctx.source_binding is None:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "승인 스냅샷(source_binding)이 없다")
    backend = ctx.build_backend
    if backend == "local":
        local = build_local_tier(tier, ctx)
        return BuildImageOutput(
            release_artifacts=local.artifacts,
            candidate_sha=local.revision,
            build_id=local.build_id,
            source=Source.FIXTURE if fake else Source.LIVE,
        )
    if backend != "codebuild":
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "build_backend은 codebuild/local만 허용한다")
    cloud = _cloud(ctx)
    if fake:
        # FakeCodeBuild는 소스를 받지 않는다. 로컬(file://) 리허설 저장소·SHA-256 커밋도 통과
        repo_url: str | None = FAKE_REPO_URL
        commit = ctx.candidate_sha if _is_sha1(ctx.candidate_sha) else _fake_sha(ctx.run_id)
    else:
        repo_url, commit = ctx.repo_url, ctx.candidate_sha
    project = cloud.get("codebuild_project_name") or (FAKE_PROJECT if fake else None)
    repository = cloud.get("image_repository") or (FAKE_IMAGE_REPOSITORY if fake else None)
    if not repo_url or not commit:
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED,
            "빌드할 앱 저장소 URL 또는 후보 커밋(candidate_sha)이 없다",
        )
    if not isinstance(project, str) or not isinstance(repository, str):
        raise DdakToolError(
            ErrorCode.INFRA_MISSING,
            "인프라 출력 codebuild_project_name 또는 image_repository가 없다",
        )
    client: CodeBuildClient = FakeCodeBuild() if fake else _codebuild(cloud)
    result = build_tier(
        client,
        tier=tier,
        current=ctx.release_artifacts,
        project=project,
        source=GitSource(repository_url=repo_url, commit_sha=commit),
        release_id=ctx.run_id,
        image_repo=docker_hub_repo(repository),
        snapshot=ctx.source_binding,
        deadline=ctx.deadline if ctx.deadline is not None else time.monotonic() + DEFAULT_BUDGET_S,
        **({"poll_s": 0.0, "sleep": lambda _s: None} if fake else {}),
    )
    return BuildImageOutput(
        release_artifacts=result.artifacts,
        candidate_sha=result.revision,
        build_id=result.build_id,
        source=Source.FIXTURE if fake else Source.LIVE,
    )


def _cloud(ctx: RunContext) -> Mapping[str, Any]:
    value = ctx.platform.get("cloud")
    return value if isinstance(value, Mapping) else {}


def _codebuild(cloud: Mapping[str, Any]) -> CodeBuildClient:
    region = cloud.get("region") or os.environ.get("AWS_REGION") or DEFAULT_REGION
    config = Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 3})
    try:
        return boto3.Session(region_name=region).client("codebuild", config=config)  # pyright: ignore[reportUnknownMemberType]
    except Exception as exc:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "AWS 세션을 만들지 못했다") from exc


def _is_sha1(value: str | None) -> bool:
    return (
        bool(value) and len(value or "") == 40 and all(c in "0123456789abcdef" for c in value or "")
    )


def _fake_sha(run_id: str) -> str:
    return hashlib.sha1(run_id.encode(), usedforsecurity=False).hexdigest()


__all__ = ["build_image"]
