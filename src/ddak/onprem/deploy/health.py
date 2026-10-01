"""O1 local health: 모든 대상 replica의 실행 이미지와 HTTP 준비 상태를 검증한다."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ddak.cd.interface import ProviderResult
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.release import ImageObservation, Platform
from ddak.onprem.deploy.containers import DIGEST, fail, image_ref
from ddak.onprem.deploy.replicas import ReplicaUnhealthy, check_ready, names

if TYPE_CHECKING:
    from ddak.onprem.deploy.provider import OnPremProvider


def health_check(provider: OnPremProvider, ctx: RunContext) -> ProviderResult:
    if not ctx.images:
        raise fail("health 대상 이미지 없음")
    count = 0
    observation = None
    for tier, ref in ctx.images.items():
        with provider._session(tier, ctx) as (host, config):
            ref = image_ref(ref)
            for number, name in names(config):
                state = host.container(name)
                try:
                    if state is None:
                        raise ReplicaUnhealthy("대상 replica 없음")
                    host.owned(state, ctx.project, tier)
                    labels = state["Config"]["Labels"]
                    # 배포 시 검증한 manifest/config 대응을 소유 컨테이너 라벨에 보존한다.
                    # classic store의 Image(config digest)를 manifest digest로 취급하지 않는다.
                    digest = labels.get("ddak.platform-digest", "")
                    config_digest = labels.get("ddak.config-digest", "")
                    if (
                        labels.get("ddak.platform") != config.platform
                        or not DIGEST.fullmatch(digest)
                        or not DIGEST.fullmatch(config_digest)
                    ):
                        raise ReplicaUnhealthy("배포 digest 관측 기록 없음")
                    if ctx.release_artifacts and tier in ctx.release_artifacts.images:
                        expected = ctx.release_artifacts.images[tier].platform_digests[
                            config.platform
                        ]
                        if expected != digest:
                            raise ReplicaUnhealthy("빌드 산출물 digest 불일치")
                    observation = ImageObservation(
                        platform=cast(Platform, config.platform), platform_digest=digest
                    )
                    check_ready(
                        host,
                        config,
                        state,
                        {"ConfigDigest": config_digest},
                        observation,
                        ref,
                        ctx.project,
                        tier,
                    )
                except ReplicaUnhealthy as error:
                    return ProviderResult(
                        provider=provider.name,
                        function="health_check",
                        passed=False,
                        detail=f"{tier} replica {number}: {error}",
                    )
                count += 1
    return ProviderResult(
        provider=provider.name,
        function="health_check",
        passed=True,
        observation=observation,
        detail=f"준비 완료 replica: {count}",
    )
