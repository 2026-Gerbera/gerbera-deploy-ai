"""cloud/deploy/ecs.py: ECS Fargate 배포·롤백. 담당 안승환(C2).

태스크 정의 새 리비전(이미지 = RunContext.images의 digest 고정 참조, secrets valueFrom = 인프라
출력의 시크릿 ARN) -> update_service. boto3는 호출마다 새 Session. AI import 금지.
"""

from __future__ import annotations

from ddak.cd.interface import ProviderResult
from ddak.core.contracts.context import RunContext

_TODO = "cloud/deploy 미구현: 담당 안승환"


def deploy_service(tier: str, ctx: RunContext) -> ProviderResult:
    """tier 서비스를 새 리비전으로 바꾼다. 출력: ProviderResult(function="deploy")."""
    raise NotImplementedError(_TODO)


def rollback_service(tier: str, ctx: RunContext) -> ProviderResult:
    """이전 리비전(ctx.previous_release)으로 되돌린다. 출력: ProviderResult(function="rollback")."""
    raise NotImplementedError(_TODO)
