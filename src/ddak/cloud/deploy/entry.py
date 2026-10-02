"""AwsProvider가 부르는 ctx 진입점(deploy·rollback·inject_config·migrate_db). 담당 안승환(C2).

ctx에서 값을 꺼내 내부 API(ecs·secrets·database)를 부르고 ProviderResult로 돌려준다. AI import 금지.
- 이미지: ctx.images[tier](digest 고정). 관측: 새 리비전으로 실행 중인 태스크의 실제 digest.
- 인프라 값: ctx.platform['cloud'](_platform.py, 💭 일부 키는 가정).
- 롤백: 온프렘과 같이 ctx.previous_release['cloud']['images'][tier]. 이전 기록이 없으면(최초 배포)
  서비스를 0개로 줄인다. DB 역마이그레이션은 하지 않는다.
- detail에 ARN·계정 ID·비밀값을 넣지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from ddak.cd.interface import ProviderName, ProviderResult
from ddak.cloud.deploy import _aws, _platform
from ddak.cloud.deploy.database import run_migration_phases
from ddak.cloud.deploy.ecs import Running, register_revision, replace_image, running_image
from ddak.cloud.deploy.ecs import scale_to_zero as _scale_to_zero
from ddak.cloud.deploy.secrets import fill_secrets
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.release import ImageArtifact, ImageObservation, Platform

PROVIDER = ProviderName.AWS.value


def deploy_service(tier: str, ctx: RunContext) -> ProviderResult:
    """tier 서비스를 ctx.images[tier]로 바꾸고 실제 실행 digest를 관측한다."""
    ref = ctx.images.get(tier)
    if not ref:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, f"배포할 {tier} 이미지가 없다")
    target = _platform.service(ctx, tier)
    ecs = _aws.client("ecs", ctx)
    revision = replace_image(ecs, target, ref, _aws.deadline(ctx))
    running = running_image(ecs, target, revision.task_definition)
    artifact = ctx.release_artifacts.images.get(tier) if ctx.release_artifacts else None
    return ProviderResult(
        provider=PROVIDER,
        function="deploy",
        changed=revision.changed,
        image_ref=ref,
        previous_image=revision.previous_image,
        observation=_observe(running, artifact),
        detail="새 리비전 배포 완료" if revision.changed else "이미 같은 이미지가 배포돼 있다",
    )


def rollback_service(tier: str, ctx: RunContext) -> ProviderResult:
    """이전 릴리스 이미지로 되돌린다. 이전 기록이 없으면 서비스를 0개로 줄인다."""
    target = _platform.service(ctx, tier)
    previous_ref = _previous_image(ctx, tier)
    ecs = _aws.client("ecs", ctx)
    if previous_ref is None:
        changed = _scale_to_zero(ecs, target, _aws.deadline(ctx))
        return ProviderResult(
            provider=PROVIDER,
            function="rollback",
            changed=changed,
            detail="이전 릴리스가 없어 서비스를 0개로 줄였다",
        )
    revision = replace_image(ecs, target, previous_ref, _aws.deadline(ctx))
    return ProviderResult(
        provider=PROVIDER,
        function="rollback",
        changed=revision.changed,
        image_ref=previous_ref,
        previous_image=revision.previous_image,
        detail="이전 릴리스 이미지로 복구",
    )


def put_secret_values(keys: Sequence[str], ctx: RunContext) -> ProviderResult:
    """keys(이름만)의 값을 채운다. 값은 결과·로그에 없다."""
    result = fill_secrets(_aws.client("secretsmanager", ctx), keys, _platform.secret_ids(ctx))
    return ProviderResult(
        provider=PROVIDER,
        function="inject_config",
        changed=result.changed,
        keys=result.keys,
        detail=f"새로 만든 키: {', '.join(result.generated)}" if result.generated else "",
    )


def run_migrations(migrations: Sequence[str], ctx: RunContext) -> ProviderResult:
    """새 WAS 이미지 리비전으로 일회성 태스크를 돌려 마이그레이션한다(서비스 교체 전)."""
    ref = ctx.images.get("was")
    if not ref:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "마이그레이션할 was 이미지가 없다")
    ecs = _aws.client("ecs", ctx)
    revision = register_revision(ecs, _platform.service(ctx, "was"), ref)
    migration = run_migration_phases(
        ecs,
        _aws.client("logs", ctx),
        _platform.migration_task(ctx, revision.task_definition),
        migrations,
        _aws.deadline(ctx),
    )
    applied = any(p.get("applied") for p in migration["phases"] if p.get("phase") == "up")
    return ProviderResult(
        provider=PROVIDER,
        function="migrate_db",
        changed=applied,
        image_ref=ref,
        migration=migration,
    )


def _observe(running: Running, artifact: ImageArtifact | None) -> ImageObservation | None:
    """실행 중 digest가 이번 빌드 결과에 속하는지 확인하고 관측값을 만든다.

    💭 ECS가 index 참조로 띄운 태스크의 imageDigest가 index인지 플랫폼 manifest인지 실제 AWS에서
    확인 필요. 둘 다 받되, index면 태스크 플랫폼의 manifest digest를 관측값으로 쓴다.
    """
    if artifact is None:
        return None
    platform = cast(Platform, running.platform)
    expected = artifact.platform_digests[platform]
    if not running.image_digests <= {expected, artifact.index_digest}:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "실행 중 이미지 digest가 빌드 결과와 다르다")
    return ImageObservation(platform=platform, platform_digest=expected)


def _previous_image(ctx: RunContext, tier: str) -> str | None:
    previous = ctx.previous_release.get("cloud")
    if previous is None:
        return None
    if not isinstance(previous, Mapping):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "이전 릴리스 형식 오류")
    images = previous.get("images")
    if not isinstance(images, Mapping) or tier not in images:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "이전 릴리스의 tier 이미지가 없다")
    value = images[tier]
    if value is None:
        return None
    if not isinstance(value, str):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "이전 릴리스 이미지 형식 오류")
    return value
