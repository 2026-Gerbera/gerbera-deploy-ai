"""빌드 한 번 → 릴리스 산출물(ReleaseArtifacts). 담당 C2. AI 없음.

build_image 툴의 본체가 될 내부 API다. 툴 입출력 계약·RunContext 필드가 정해지기 전이라
값(저장소 URL, 커밋 SHA, 스냅샷, 이전 이미지)은 모두 인자로 받는다(💭 정준우와 연결 확정 필요).

- 바뀐 tier는 CodeBuild 한 번에 같이 빌드한다(tier마다 빌드를 따로 띄우지 않음, 동시 빌드 한도).
- 바뀌지 않은 tier는 이전 릴리스의 이미지를 그대로 넣는다. 실행기는 툴 출력의
  release_artifacts로 컨텍스트를 통째로 바꾸므로 결과에 모든 tier가 있어야 한다.
- 스냅샷은 승인된 것(실행기 p.snapshot)을 그대로 붙인다. 실행기가 다시 대조한다.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from ddak.cloud.build.codebuild import BuildSource, CodeBuildClient, run_build
from ddak.cloud.build.registries import ImageRegistry, image_artifact
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.release import ImageArtifact, ReleaseArtifacts, SnapshotBinding


@dataclass(frozen=True)
class ReleaseBuild:
    artifacts: ReleaseArtifacts
    build_id: str | None  # 새로 빌드한 tier가 없으면 None
    revision: str  # 이미지 revision 라벨과 같은 ai-prod 커밋 SHA


def build_release(
    client: CodeBuildClient,
    *,
    project: str,
    source: BuildSource,
    tiers: Sequence[str],
    release_id: str,
    registry: ImageRegistry,
    repository: str,
    snapshot: SnapshotBinding,
    deadline: float,
    unchanged: Mapping[str, ImageArtifact] | None = None,
    poll_s: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ReleaseBuild:
    """tiers(바뀐 tier)를 빌드하고 unchanged(이전 이미지)와 합쳐 릴리스 산출물을 만든다."""
    kept = dict(unchanged or {})
    overlap = sorted(set(tiers) & kept.keys())
    if overlap:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            f"빌드할 tier와 이전 이미지 tier가 겹친다: {', '.join(overlap)}",
        )
    if not tiers and not kept:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "빌드할 tier도 이전 이미지도 없다")

    images: dict[str, ImageArtifact] = dict(kept)
    build_id: str | None = None
    if tiers:
        result = run_build(
            client,
            project,
            source,
            tiers,
            release_id,
            deadline,
            poll_s=poll_s,
            clock=clock,
            sleep=sleep,
        )
        build_id = result.build_id
        for tier, digests in result.digests.items():
            images[tier] = image_artifact(
                registry, repository, digests.index_digest, digests.platform_digests
            )
    try:
        artifacts = ReleaseArtifacts(snapshot=snapshot, images=images)
    except ValidationError as exc:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "릴리스 산출물 형식이 아니다") from exc
    return ReleaseBuild(artifacts=artifacts, build_id=build_id, revision=source.revision)


__all__ = ["ReleaseBuild", "build_release"]
