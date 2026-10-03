"""AwsProvider가 부르는 ctx 진입점(deploy·rollback·inject_config·migrate_db). 담당 안승환(C2).

ctx에서 값을 꺼내 내부 API(ecs·secrets·database)를 부르고 ProviderResult로 돌려준다. AI import 금지.
- 이미지: ctx.images(digest 고정). R9 (a)라 web·was가 한 태스크에 있다. 계획은 deploy.<tier> step을
  tier마다 부르지만, 첫 step이 이번 run의 컨테이너 이미지를 모두 한 리비전으로 한 번에 전환하고
  다음 step은 이미 반영돼 있어 바꾸지 않는다(서비스 전환 1회). 관측은 step의 tier 컨테이너만.
- 인프라 값: ctx.platform['cloud'](_platform.py). 컨테이너 이름은 tier와 같은 web·was.
- 관측 기준: 이번 빌드 산출물(ctx.release_artifacts)에 tier가 있으면 그것, 없으면 이월 이미지
  ctx.previous_release['cloud']['image_sources'][tier](CarriedImageSource, 후속 수정 7).
  둘 다 없거나 ref가 ctx.images와 다르면 실패한다. 이월 산출물은 새 ReleaseArtifacts에 넣지 않는다.
- 런타임 설정(앱 계약: 앱 저장소 env.example·nginx 템플릿, 10/3 정준우 요청). Terraform은 컨테이너
  env를 두지 않으므로 이 층이 넣는다. was는 공개 설정 env(RUNTIME_ENV_KEYS, SESSION_COOKIE_SECURE
  =true) + SECRET_KEY·DATABASE_URL 시크릿 ARN(valueFrom), web은 WAS_UPSTREAM(같은 태스크라
  127.0.0.1:8000). 💭 앱 계정(flaskr_app)·migrator URL 분리는 DB 계정 생성 주체가 정해진 뒤.
  RELEASE_ID는 배포면 ctx.run_id, 롤백이면 이전 릴리스의 release_id(온프렘과 같다).
  마이그레이션 태스크도 같은 값을 쓴다.
- inject_config: 시크릿 출력(app_secret_arn_<KEY>)이 있는 키만 채운다. SECRET_KEY는 요청 키에
  없어도 출력이 있으면 함께 채운다(태스크가 valueFrom으로 읽으므로 비어 있으면 기동 실패). 난수,
  DATABASE_URL은 RDS 출력(rds_endpoint, rds_master_secret_arn)으로 만든다. 공개 설정 키는 배포가
  태스크 정의에 넣으므로 건너뛴다. 둘 다 아닌 키는 INFRA_MISSING.
- 롤백: 진행 중 ECS 배포가 있으면 먼저 ECS 롤백(ecs.rollback_in_flight, 자동 롤백 중이면 대기만).
  그 뒤 온프렘과 같이 ctx.previous_release['cloud']['images']로 맞춘다(이미 같으면 바꾸지 않음).
  이전 기록이 없으면(최초 배포) 서비스를 0개로 줄인다. DB 역마이그레이션은 하지 않는다.
- 런타임 시크릿에는 마이그레이션 계정 키(*_MIGRATOR)를 넣지 않는다(core.env_keys).
- detail에 ARN·계정 ID·비밀값을 넣지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast
from urllib.parse import quote

from pydantic import ValidationError

from ddak.cd.interface import ProviderName, ProviderResult
from ddak.cloud.deploy import _aws, _platform
from ddak.cloud.deploy.database import run_migration_phases
from ddak.cloud.deploy.ecs import (
    EcsService,
    Running,
    register_revision,
    replace_images,
    rollback_in_flight,
    running_image,
)
from ddak.cloud.deploy.ecs import scale_to_zero as _scale_to_zero
from ddak.cloud.deploy.secrets import fill_secrets
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.infra_outputs import checked_outputs
from ddak.core.contracts.release import (
    CarriedImageSource,
    ImageArtifact,
    ImageObservation,
    Platform,
)
from ddak.core.env_keys import check_runtime_keys
from ddak.core.project_settings import cloud_platform_name
from ddak.core.storage import OUTPUT_KEY, STORAGE_ENV_KEY

PROVIDER = ProviderName.AWS.value
RELEASE_ID = "RELEASE_ID"
WAS_UPSTREAM = "127.0.0.1:8000"  # awsvpc: 같은 태스크 컨테이너는 localhost 공유, was는 8000
# 배포 층이 태스크 정의 env로 넣는 공개 설정 키(was). inject_config는 이 키들을 건너뛴다.
RUNTIME_ENV_KEYS = frozenset(
    {"APP_BASE_URL", "APP_ENV", "MIGRATE_MODE", "PROXY_FIX_X_FOR", "PROXY_FIX_X_PROTO",
     "SESSION_COOKIE_SECURE", RELEASE_ID, "SOURCE_SHA", STORAGE_ENV_KEY}
)  # fmt: skip
DATABASE_URL = "DATABASE_URL"
SECRET_KEY = "SECRET_KEY"  # noqa: S105 (키 이름, 비밀값 아님)
# RDS CA 번들은 앱 이미지 /app/certs/global-bundle.pem(앱 저장소 certs/, flaskr/db.py 형식)
_DB_URL_QUERY = "?charset=utf8mb4&ssl_ca=/app/certs/global-bundle.pem"


def deploy_service(tier: str, ctx: RunContext) -> ProviderResult:
    """이번 run의 컨테이너 이미지를 한 번에 전환하고 tier 컨테이너의 실제 실행 digest를 관측한다."""
    if not ctx.images.get(tier):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, f"배포할 {tier} 이미지가 없다")
    images = _container_images(ctx.images, required=tier)
    name = _platform.container(tier)
    artifact = _artifact(ctx, tier)
    target = _platform.service(ctx)
    ecs = _aws.client("ecs", ctx)
    revision = replace_images(
        ecs,
        target,
        images,
        _aws.deadline(ctx),
        desired_count=_platform.DESIRED_COUNT,
        environment=_runtime_environment(ctx, ctx.run_id, ctx.source_sha),
        secrets=_runtime_secrets(ctx),
        elb=_elb(ctx, target),
        expected_current=_previous_images(ctx),
    )
    running = running_image(ecs, target, name, revision.task_definition)
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
    previous = _previous_release(ctx, tier)
    ecs = _aws.client("ecs", ctx)
    in_flight = rollback_in_flight(ecs, target, _aws.deadline(ctx))
    if previous is None:
        changed = _scale_to_zero(ecs, target, _aws.deadline(ctx))
        return ProviderResult(
            provider=PROVIDER,
            function="rollback",
            changed=changed or in_flight is not None,
            detail=_join(in_flight, "이전 릴리스가 없어 서비스를 0개로 줄였다"),
        )
    previous_images, release_id, source_sha = previous
    images = _container_images(previous_images, required=tier)
    revision = replace_images(
        ecs,
        target,
        images,
        _aws.deadline(ctx),
        desired_count=_platform.DESIRED_COUNT,
        environment=_runtime_environment(ctx, release_id, source_sha),
        secrets=_runtime_secrets(ctx),
        elb=_elb(ctx, target),
    )
    name = _platform.container(tier)
    return ProviderResult(
        provider=PROVIDER,
        function="rollback",
        changed=revision.changed or in_flight is not None,
        image_ref=images[name],
        previous_image=revision.previous_images.get(name),
        detail=_join(
            in_flight,
            "이전 릴리스 이미지로 복구" if revision.changed else "이미 이전 릴리스로 실행 중",
        ),
    )


def _join(*parts: str | None) -> str:
    return "; ".join(p for p in parts if p)


def _elb(ctx: RunContext, target: EcsService) -> Any:
    """대상 그룹 출력이 있을 때만 ALB 클라이언트(트래픽 전환 시점 완료 판정용)."""
    return _aws.client("elbv2", ctx) if target.target_group else None


def _previous_images(ctx: RunContext) -> dict[str, str] | None:
    """직전 성공 클라우드 릴리스의 {컨테이너: 참조}. 첫 배포면 None(확인하지 않음)."""
    previous: Any = ctx.previous_release.get("cloud")
    images = previous.get("images") if isinstance(previous, Mapping) else None
    if not isinstance(images, Mapping):
        return None
    return {
        _platform.container(t): ref
        for t, ref in images.items()
        if _platform.has_container(t) and isinstance(ref, str)
    }


def put_secret_values(keys: Sequence[str], ctx: RunContext) -> ProviderResult:
    """keys(이름만)의 값을 채운다. 값은 결과·로그에 없다."""
    check_runtime_keys(keys)
    secret_ids = _platform.secret_ids(ctx)
    secret_keys = [k for k in keys if k in secret_ids or k not in RUNTIME_ENV_KEYS]
    if secret_keys and SECRET_KEY in secret_ids and SECRET_KEY not in secret_keys:
        secret_keys.append(SECRET_KEY)  # 태스크 정의가 valueFrom으로 읽는다(비면 기동 실패)
    if not secret_keys:
        return ProviderResult(
            provider=PROVIDER,
            function="inject_config",
            changed=False,
            keys=list(keys),
            detail="공개 설정 키는 배포 때 태스크 정의에 넣는다",
        )
    client = _aws.client("secretsmanager", ctx)
    generated: dict[str, str] = {}
    if DATABASE_URL in secret_keys:
        generated[DATABASE_URL] = _database_url(client, ctx)
    result = fill_secrets(client, secret_keys, secret_ids, generated_values=generated)
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
    was = _platform.container("was")
    revision = register_revision(
        ecs,
        _platform.service(ctx),
        {was: ref},
        environment=_runtime_environment(ctx, ctx.run_id, ctx.source_sha),
        secrets=_runtime_secrets(ctx),
        only_containers=frozenset({was}),
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


def _runtime_environment(
    ctx: RunContext, release_id: str, source_sha: str | None
) -> dict[str, dict[str, str]]:
    """{컨테이너: env}. 서비스와 선행 마이그레이션이 같은 공개 런타임 설정을 쓴다."""
    if not ctx.cloud_domain:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "cloud_domain이 설정되지 않았다")
    was = {
        "APP_BASE_URL": f"https://{ctx.cloud_domain}",
        "APP_ENV": "production",
        "MIGRATE_MODE": "up",
        "PROXY_FIX_X_FOR": "1",  # ALB → nginx → was. nginx가 XFF를 이어 붙인다
        "PROXY_FIX_X_PROTO": "1",
        "SESSION_COOKIE_SECURE": "true",  # APP_BASE_URL이 https라서
        RELEASE_ID: release_id,
        "SOURCE_SHA": source_sha or "unknown",
    }
    upload_bucket = ctx.platform.get("cloud", {}).get(OUTPUT_KEY)
    if upload_bucket:
        try:
            checked_outputs({OUTPUT_KEY: upload_bucket}, "app")
        except ValueError:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "업로드 버킷 출력 형식 오류") from None
        was[STORAGE_ENV_KEY] = f"s3://{upload_bucket}/img"
    return {
        _platform.container("was"): was,
        _platform.container("web"): {"WAS_UPSTREAM": WAS_UPSTREAM},
    }


def _runtime_secrets(ctx: RunContext) -> dict[str, dict[str, str]]:
    """비밀값은 평문 대신 시크릿 ARN(valueFrom)으로만 태스크 정의에 넣는다."""
    ids = _platform.secret_ids(ctx)
    missing = [k for k in (SECRET_KEY, DATABASE_URL) if not ids.get(k)]
    if missing:
        raise DdakToolError(
            ErrorCode.INFRA_MISSING,
            f"시크릿 출력(app_secret_arn_<KEY>)이 없다: {', '.join(missing)}",
        )
    return {_platform.container("was"): {k: ids[k] for k in (SECRET_KEY, DATABASE_URL)}}


def _database_url(client: Any, ctx: RunContext) -> str:
    """RDS 관리 master 자격증명으로 앱 DATABASE_URL을 만든다. 값은 반환만 하고 남기지 않는다.

    💭 데모 단순화: 앱이 master 계정을 쓴다(온프렘은 앱·마이그레이터 계정 분리). DB 이름은
    클라우드 플랫폼 이름(generate_infra가 RDS db_name = var.project로 만든다. 기본은 프로젝트 이름).
    """
    master_arn, endpoint = _platform.rds(ctx)
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
    if not ctx.project:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "프로젝트 이름(DB 이름)이 없다")
    database = quote(cloud_platform_name(ctx.project, ctx.project_settings), safe="")
    return f"mysql+pymysql://{username}:{password}@{endpoint}/{database}{_DB_URL_QUERY}"


def _observe(running: Running, artifact: ImageArtifact) -> ImageObservation:
    """실행 중 digest가 기준 산출물(이번 빌드 또는 이월)에 속하는지 확인하고 관측값을 만든다.

    💭 ECS가 index 참조로 띄운 태스크의 imageDigest가 index인지 플랫폼 manifest인지 실제 AWS에서
    확인 필요. 둘 다 받되, index면 태스크 플랫폼의 manifest digest를 관측값으로 쓴다.
    """
    platform = cast(Platform, running.platform)
    expected = artifact.platform_digests[platform]
    if not running.image_digests <= {expected, artifact.index_digest}:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "실행 중 이미지 digest가 빌드 결과와 다르다")
    return ImageObservation(platform=platform, platform_digest=expected)


def _artifact(ctx: RunContext, tier: str) -> ImageArtifact:
    """관측 기준 산출물: 이번 빌드 결과, 없으면 이월 이미지. ref가 배포할 이미지와 같아야 한다."""
    artifact = ctx.release_artifacts.images.get(tier) if ctx.release_artifacts else None
    if artifact is None:
        artifact = _carried(ctx, tier)
    if artifact.ref != ctx.images[tier]:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, f"{tier} 산출물과 배포할 이미지가 다르다")
    return artifact


def _carried(ctx: RunContext, tier: str) -> ImageArtifact:
    previous: Any = ctx.previous_release.get("cloud")
    try:
        origin = previous["image_sources"][tier]
        return CarriedImageSource.model_validate(origin).artifact
    except (KeyError, TypeError, ValidationError):
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED,
            f"{tier}: 이번 빌드 결과도 이월 이미지 산출물도 없다",
        ) from None


def _container_images(tier_images: Mapping[str, str], *, required: str) -> dict[str, str]:
    """{tier: 참조} → {컨테이너: 참조}. 클라우드 태스크에 없는 tier(예: db)는 건너뛴다."""
    return {
        _platform.container(t): ref
        for t, ref in tier_images.items()
        if t == required or _platform.has_container(t)
    }


def _previous_release(ctx: RunContext, tier: str) -> tuple[dict[str, str], str, str | None] | None:
    """이전 릴리스의 ({tier: 참조}, release_id, source_sha).

    이전 기록이 없거나 이 tier가 처음 배포였으면 None.
    """
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
    release_id = previous.get("release_id")
    if not isinstance(release_id, str) or not release_id or any(c in release_id for c in "\r\n\0"):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "이전 릴리스 RELEASE_ID 형식 오류")
    source_sha = previous.get("source_sha")
    return result, release_id, source_sha if isinstance(source_sha, str) else None
