"""O1 local health: 모든 대상 replica의 실행 이미지와 HTTP 준비 상태를 검증한다."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ddak.cd.interface import ProviderResult
from ddak.core.contracts.context import RunContext
from ddak.onprem.deploy.containers import fail, image_ref
from ddak.onprem.deploy.replicas import check_ready, names

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
            observation, local = host.image(ref, config.platform)
            if (
                ctx.release_artifacts
                and tier in ctx.release_artifacts.images
                and observation.platform_digest
                != ctx.release_artifacts.images[tier].platform_digests[observation.platform]
            ):
                raise fail("health 빌드 산출물 digest 불일치")
            for _, name in names(config):
                state = host.container(name)
                if state is None:
                    raise fail("health 대상 replica 없음")
                check_ready(host, config, state, local, observation, ref, ctx.project, tier)
                count += 1
    return ProviderResult(
        provider=provider.name,
        function="health_check",
        passed=True,
        observation=observation,
        detail=f"준비 완료 replica: {count}",
    )
