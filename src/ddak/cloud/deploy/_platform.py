"""ctx.platform['cloud'](Terraform 출력)에서 C2가 읽는 값. 키 이름은 이 파일에만 둔다.

출력 이름은 후속 수정 7(harness/docs/decisions/2026-10-02-exec-fix7.md)에서 확정됐다.
- 플랫폼 출력: cluster_name, ecs_service_name(서비스 1개, R9 (a)), public_subnet_ids,
  app_security_group_id, rds_endpoint, rds_master_secret_arn, app_secret_arn_<KEY>
  (SECRET_KEY·DATABASE_URL).
- 컨테이너 이름은 출력이 아니다. tier 이름과 같은 web·was로 고정이고, 태스크 정의에 그 이름의
  컨테이너가 있는지는 ecs.register_revision이 확인한다.
- region도 출력이 아니다. ddak.app이 cloud 컨텍스트에 넣고, 없으면 ap-northeast-2(_aws.client).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ddak.cloud.deploy.database import MigrationTask, TaskNetwork
from ddak.cloud.deploy.ecs import EcsService
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode

KEY_CLUSTER = "cluster_name"
KEY_SERVICE = "ecs_service_name"  # R9 (a): web·was가 한 서비스·한 태스크
KEY_SUBNETS = "public_subnet_ids"  # 💭 Fargate 퍼블릭 서브넷 + 퍼블릭 IP(Docker Hub pull)
KEY_SECURITY_GROUP = "app_security_group_id"
KEY_RDS_ENDPOINT = "rds_endpoint"  # host:port
KEY_RDS_MASTER_SECRET = "rds_master_secret_arn"  # noqa: S105 (출력 이름, 비밀값 아님)
KEY_REGION = "region"  # _aws.client가 읽는다(출력 아님, 앱이 주입)
OUTPUT_SECRET_ARN_PREFIX = "app_secret_arn_"  # noqa: S105 (출력 이름 접두사, 비밀값 아님)

DESIRED_COUNT = 2  # 💭 R11 제안값. 첫 배포(Terraform이 0개로 만든 서비스) 때 올릴 태스크 수

CONTAINERS = frozenset({"web", "was"})  # 태스크 정의 컨테이너 이름 = tier 이름(후속 수정 7)


def cloud(ctx: RunContext) -> Mapping[str, Any]:
    value = ctx.platform.get("cloud")
    if not isinstance(value, Mapping):
        raise DdakToolError(ErrorCode.INFRA_MISSING, "클라우드 환경 정보(Terraform 출력)가 없다")
    return value


def _text(platform: Mapping[str, Any], key: str) -> str:
    value = platform.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DdakToolError(ErrorCode.INFRA_MISSING, f"클라우드 환경 정보에 {key}가 없다")
    return value


def _texts(platform: Mapping[str, Any], key: str) -> list[str]:
    value = platform.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise DdakToolError(ErrorCode.INFRA_MISSING, f"클라우드 환경 정보에 {key}가 없다")
    return list(value)


def service(ctx: RunContext) -> EcsService:
    platform = cloud(ctx)
    return EcsService(cluster=_text(platform, KEY_CLUSTER), service=_text(platform, KEY_SERVICE))


def container(tier: str) -> str:
    """tier의 컨테이너 이름(tier와 같다). 클라우드 태스크에 없는 tier(예: db는 RDS)는 거부한다."""
    if tier not in CONTAINERS:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, f"클라우드 태스크에 {tier} 컨테이너가 없다")
    return tier


def has_container(tier: str) -> bool:
    """이 tier가 클라우드 태스크에 컨테이너로 있는가(예: db는 RDS라 없다)."""
    return tier in CONTAINERS


def rds(ctx: RunContext) -> tuple[str, str]:
    """(RDS 관리 master 시크릿 ARN, 엔드포인트 host:port)."""
    platform = cloud(ctx)
    return _text(platform, KEY_RDS_MASTER_SECRET), _text(platform, KEY_RDS_ENDPOINT)


def migration_task(ctx: RunContext, task_definition: str) -> MigrationTask:
    platform = cloud(ctx)
    return MigrationTask(
        cluster=service(ctx).cluster,
        task_definition=task_definition,
        container=container("was"),
        network=TaskNetwork(
            subnets=_texts(platform, KEY_SUBNETS),
            security_groups=[_text(platform, KEY_SECURITY_GROUP)],
        ),
    )


def secret_ids(ctx: RunContext) -> dict[str, str]:
    """{키: 시크릿 ARN}. 값이 아니라 위치만이다."""
    return {
        name[len(OUTPUT_SECRET_ARN_PREFIX) :]: value
        for name, value in cloud(ctx).items()
        if name.startswith(OUTPUT_SECRET_ARN_PREFIX) and isinstance(value, str) and value
    }
