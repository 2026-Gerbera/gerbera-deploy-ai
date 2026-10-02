"""ctx.platform['cloud'](Terraform 출력)에서 C2가 읽는 값. 키 이름은 이 파일에만 둔다.

💭 아래 키 중 일부는 아직 Terraform 출력 허용 목록(core/contracts/infra_outputs.py, PR #8)에 없다.
구축되어 있다고 가정한 이름이며, 정준우와 확정하면 이 파일만 고친다.
- 있음: cluster_name, public_subnet_ids, app_secret_arn_<KEY>
- 가정(추가 요청): ecs_service_name(서비스 1개, R9 (a)), ecs_container_name_<TIER>,
  app_security_group_id, region
참고: C3 cloud/health는 ecs_cluster·ecs_service(서비스 1개)·region을 읽는다. 이름 통일 필요.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ddak.cloud.deploy.database import MigrationTask, TaskNetwork
from ddak.cloud.deploy.ecs import EcsService
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode

KEY_CLUSTER = "cluster_name"
KEY_SERVICE = "ecs_service_name"  # R9 (a): web·was가 한 서비스·한 태스크
KEY_CONTAINER = "ecs_container_name_{tier}"
KEY_SUBNETS = "public_subnet_ids"  # 💭 Fargate 퍼블릭 서브넷 + 퍼블릭 IP(Docker Hub pull)
KEY_SECURITY_GROUP = "app_security_group_id"
KEY_REGION = "region"  # _aws.client가 읽는다
OUTPUT_SECRET_ARN_PREFIX = "app_secret_arn_"  # noqa: S105 (출력 이름 접두사, 비밀값 아님)

DESIRED_COUNT = 2  # 💭 R11 제안값. 첫 배포(Terraform이 0개로 만든 서비스) 때 올릴 태스크 수

_TIER_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


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


def container(ctx: RunContext, tier: str) -> str:
    if not _TIER_KEY.fullmatch(tier):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "tier 이름 형식이 아니다")
    value = cloud(ctx).get(KEY_CONTAINER.format(tier=tier), tier)
    if not isinstance(value, str) or not value:
        raise DdakToolError(ErrorCode.INFRA_MISSING, f"{tier} 컨테이너 이름이 없다")
    return value


def has_container(ctx: RunContext, tier: str) -> bool:
    """이 tier가 클라우드 태스크에 컨테이너로 있는가(예: db는 RDS라 없다)."""
    return bool(_TIER_KEY.fullmatch(tier)) and tier in {"web", "was"}


def migration_task(ctx: RunContext, task_definition: str) -> MigrationTask:
    platform = cloud(ctx)
    return MigrationTask(
        cluster=service(ctx).cluster,
        task_definition=task_definition,
        container=container(ctx, "was"),
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
