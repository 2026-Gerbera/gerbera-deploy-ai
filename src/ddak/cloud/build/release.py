"""빌드 한 번 → 릴리스 산출물(ReleaseArtifacts). 담당 C2. AI 없음.

build_image 툴의 본체가 될 내부 API다. 툴 입출력 계약이 정해지기 전이라 값은 인자로 받는다.
PR #8(정준우) 기준 툴 연결:
- 소스 = GitSource(ctx.project_settings["repo_url"], ctx.candidate_sha), release_id = ctx.run_id
- 앞선 build step 결과 = ctx.release_artifacts
- image_repo = 인프라 출력 image_repository(💭 추가 예정)
- 승인 스냅샷은 정준우가 RunContext에 필드를 추가한 뒤 연결한다.

- 계획은 tier마다 build.<tier> step을 둔다. step 하나 = build_tier 한 번 = CodeBuild 한 번.
- 실행기는 툴 출력의 release_artifacts로 컨텍스트를 통째로 바꾼다. 그래서 build_tier는 앞선
  build step이 만든 이미지(current)에 이번 tier를 더해 돌려준다.
- 이번 run에서 빌드하지 않은 tier(변경 없음)는 넣지 않는다. 그 tier는 배포 step도 없다.
- push는 빌드 안에서 끝난다. push_image 툴은 구현·등록하지 않는다(계획이 부르지 않음).
- 스냅샷은 승인된 것(실행기 p.snapshot)을 그대로 붙인다. 실행기가 다시 대조한다.
- image_repo(docker.io/<네임스페이스>/<저장소>) 하나로 push 대상(override)과 결과 이미지 주소를 같이
  만든다. 출처가 둘이면 기록이 이미지가 없는 저장소를 가리킬 수 있다(PR #9 리뷰).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from ddak.cloud.build.codebuild import BuildSource, CodeBuildClient, check_image_repo, run_build
from ddak.cloud.build.registries import DockerHub, image_artifact
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
    image_repo: str,
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
    check_image_repo(image_repo)

    images: dict[str, ImageArtifact] = dict(kept)
    build_id: str | None = None
    if tiers:
        result = run_build(
            client,
            project,
            source,
            tiers,
            release_id,
            image_repo,
            deadline,
            poll_s=poll_s,
            clock=clock,
            sleep=sleep,
        )
        build_id = result.build_id
        for tier, digests in result.digests.items():
            images[tier] = _artifact(image_repo, digests.index_digest, digests.platform_digests)
    try:
        artifacts = ReleaseArtifacts(snapshot=snapshot, images=images)
    except ValidationError as exc:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "릴리스 산출물 형식이 아니다") from exc
    return ReleaseBuild(artifacts=artifacts, build_id=build_id, revision=source.revision)


def _artifact(image_repo: str, index: str, platforms: Mapping[str, str]) -> ImageArtifact:
    """push한 저장소(image_repo)와 같은 주소로 digest 고정 산출물을 만든다."""
    _, namespace, repository = image_repo.split("/")
    artifact = image_artifact(DockerHub(namespace=namespace), repository, index, platforms)
    if artifact.ref != f"{image_repo}@{artifact.index_digest}":
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "결과 이미지 주소가 push 저장소와 다르다")
    return artifact


def build_tier(
    client: CodeBuildClient,
    *,
    tier: str,
    current: ReleaseArtifacts | None,
    project: str,
    source: BuildSource,
    release_id: str,
    image_repo: str,
    snapshot: SnapshotBinding,
    deadline: float,
    poll_s: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ReleaseBuild:
    """build.<tier> step 하나. tier를 빌드해 같은 run의 앞선 빌드 결과(current)와 합친다.

    current의 같은 tier는 이번 결과로 바꾼다(재시도 등). 다른 스냅샷의 결과는 섞지 않는다.
    """
    kept: dict[str, ImageArtifact] = {}
    if current is not None:
        if current.snapshot != snapshot:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "앞선 빌드 결과의 스냅샷이 이번 승인과 다르다"
            )
        kept = {t: a for t, a in current.images.items() if t != tier}
    return build_release(
        client,
        project=project,
        source=source,
        tiers=[tier],
        release_id=release_id,
        image_repo=image_repo,
        snapshot=snapshot,
        deadline=deadline,
        unchanged=kept,
        poll_s=poll_s,
        clock=clock,
        sleep=sleep,
    )


__all__ = ["ReleaseBuild", "build_release", "build_tier"]
