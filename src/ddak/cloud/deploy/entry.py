"""AwsProvider가 부르는 ctx 진입점(deploy·rollback·inject_config·migrate_db). 담당 안승환(C2).

ctx에서 값을 꺼내 내부 API(ecs·secrets·database)를 부르고 ProviderResult로 돌려준다. AI import 금지.
- 이미지: ctx.images(digest 고정). R9 (a)라 web·was가 한 태스크에 있다. 계획은 deploy.<tier> step을
  tier마다 부르지만, 첫 step이 이번 run의 컨테이너 이미지를 모두 한 리비전으로 한 번에 전환하고
  다음 step은 이미 반영돼 있어 바꾸지 않는다(서비스 전환 1회). 관측은 step의 tier 컨테이너만.
- 인프라 값: ctx.platform['cloud'](_platform.py, 💭 일부 키는 가정).
- 롤백: 온프렘과 같이 ctx.previous_release['cloud']['images']. 이전 기록이 없으면(최초 배포)
  서비스를 0개로 줄인다. DB 역마이그레이션은 하지 않는다.
- detail에 ARN·계정 ID·비밀값을 넣지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import cast
from urllib.parse import quote

from ddak.cd.interface import ProviderName, ProviderResult
from ddak.cloud.deploy import _aws, _platform
from ddak.cloud.deploy.database import run_migration_phases
from ddak.cloud.deploy.ecs import Running, register_revision, replace_images, running_image
from ddak.cloud.deploy.ecs import scale_to_zero as _scale_to_zero
from ddak.cloud.deploy.secrets import fill_secrets
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.release import ImageArtifact, ImageObservation, Platform

PROVIDER = ProviderName.AWS.value


def deploy_service(tier: str, ctx: RunContext) -> ProviderResult:
    """이번 run의 컨테이너 이미지를 한 번에 전환하고 tier 컨테이너의 실제 실행 digest를 관측한다."""
    if not ctx.images.get(tier):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, f"배포할 {tier} 이미지가 없다")
    images = _container_images(ctx, ctx.images, required=tier)
    target = _platform.service(ctx)
    name = _platform.container(ctx, tier)
    ecs = _aws.client("ecs", ctx)
    revision = replace_images(
        ecs,
        target,
        images,
        _aws.deadline(ctx),
        desired_count=_platform.DESIRED_COUNT,
        environment=_runtime_environment(ctx),
        secrets=_runtime_secrets(ctx),
        web_environment={"WAS_UPSTREAM": "127.0.0.1:8000"},
    )
    running = running_image(ecs, target, name, revision.task_definition)
    artifact = ctx.release_artifacts.images.get(tier) if ctx.release_artifacts else None
    return ProviderResult(
        provider=PROVIDER,
        function="deploy",
        changed=revision.changed,
        image_ref=images[name],
        previous_image=revision.previous_images.get(name),
        observation=_observe(running, artifact),
        detail="새 리비전 배포 완료" if revision.changed else "이미 이번 리비전으로 배포돼 있다",
    )


def rollback_service(tier: str, ctx: RunContext) -> ProviderResult:
    """이전 릴리스 이미지로 되돌린다. 이전 기록이 없으면 서비스를 0개로 줄인다."""
    target = _platform.service(ctx)
    previous = _previous_images(ctx, tier)
    ecs = _aws.client("ecs", ctx)
    if previous is None:
        changed = _scale_to_zero(ecs, target, _aws.deadline(ctx))
        return ProviderResult(
            provider=PROVIDER,
            function="rollback",
            changed=changed,
            detail="이전 릴리스가 없어 서비스를 0개로 줄였다",
        )
    images = _container_images(ctx, previous, required=tier)
    revision = replace_images(
        ecs, target, images, _aws.deadline(ctx), desired_count=_platform.DESIRED_COUNT
    )
    name = _platform.container(ctx, tier)
    return ProviderResult(
        provider=PROVIDER,
        function="rollback",
        changed=revision.changed,
        image_ref=images[name],
        previous_image=revision.previous_images.get(name),
        detail="이전 릴리스 이미지로 복구",
    )


def put_secret_values(keys: Sequence[str], ctx: RunContext) -> ProviderResult:
    """keys(이름만)의 값을 채운다. 값은 결과·로그에 없다."""
    client = _aws.client("secretsmanager", ctx)
    generated: dict[str, str] = {}
    if "DATABASE_URL" in keys:
        platform = _platform.cloud(ctx)
        master_arn = platform.get("rds_master_secret_arn")
        endpoint = platform.get("rds_endpoint")
        if not isinstance(master_arn, str) or not isinstance(endpoint, str):
            raise DdakToolError(ErrorCode.INFRA_MISSING, "RDS 연결 출력이 없다")
        payload = _aws.call(
            "RDS 자격증명을 읽지 못했다",
            lambda: client.get_secret_value(SecretId=master_arn),
        ).get("SecretString")
        try:
            credentials = json.loads(payload)
            username = quote(credentials["username"], safe="")
            password = quote(credentials["password"], safe="")
        except (TypeError, KeyError, ValueError):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "RDS 자격증명 형식 오류") from None
        generated["DATABASE_URL"] = (
            f"mysql+pymysql://{username}:{password}@{endpoint}/{ctx.project}"
            "?charset=utf8mb4&ssl_ca=/app/certs/global-bundle.pem"
        )
    result = fill_secrets(
        client,
        keys,
        _platform.secret_ids(ctx),
        generated_values=generated,
    )
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
    revision = register_revision(
        ecs,
        _platform.service(ctx),
        {_platform.container(ctx, "was"): ref},
        environment=_runtime_environment(ctx),
        secrets=_runtime_secrets(ctx),
        only_containers=frozenset({_platform.container(ctx, "was")}),
    )
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


def _runtime_environment(ctx: RunContext) -> dict[str, str]:
    """서비스와 선행 마이그레이션이 반드시 같은 공개 런타임 설정을 사용한다."""
    return {
        "APP_BASE_URL": f"https://{ctx.cloud_domain}",
        "APP_ENV": "production",
        "MIGRATE_MODE": "up",
        "PROXY_FIX_X_FOR": "1",
        "PROXY_FIX_X_PROTO": "1",
        "RELEASE_ID": ctx.run_id,
        "SOURCE_SHA": ctx.source_sha or "unknown",
    }


def _runtime_secrets(ctx: RunContext) -> dict[str, str]:
    """비밀값은 평문 대신 Secrets Manager ARN으로만 태스크 정의에 주입한다."""
    return {"DATABASE_URL": _platform.secret_ids(ctx)["DATABASE_URL"]}


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


def _container_images(
    ctx: RunContext, tier_images: Mapping[str, str], *, required: str
) -> dict[str, str]:
    """{tier: 참조} → {컨테이너: 참조}. 클라우드 태스크에 없는 tier(예: db)는 건너뛴다."""
    images = {
        _platform.container(ctx, t): ref
        for t, ref in tier_images.items()
        if t == required or _platform.has_container(ctx, t)
    }
    return images


def _previous_images(ctx: RunContext, tier: str) -> dict[str, str] | None:
    """이전 릴리스의 {tier: 참조}. 이전 기록이 없거나 이 tier가 처음 배포였으면 None."""
    previous = ctx.previous_release.get("cloud")
    if previous is None:
        return None
    if not isinstance(previous, Mapping):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "이전 릴리스 형식 오류")
    images = previous.get("images")
    if not isinstance(images, Mapping) or tier not in images:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "이전 릴리스의 tier 이미지가 없다")
    if images[tier] is None:
        return None
    result: dict[str, str] = {}
    for t, value in images.items():
        if value is None:
            continue
        if not isinstance(t, str) or not isinstance(value, str):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "이전 릴리스 이미지 형식 오류")
        result[t] = value
    return result
