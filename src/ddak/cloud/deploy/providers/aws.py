"""AWS ECS/Fargate CD provider."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar, Literal

from ddak.cd.interface import ProviderName, ProviderResult
from ddak.cloud.health import health_check as check_cloud_health
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode


def _todo(owner: str) -> DdakToolError:
    return DdakToolError(ErrorCode.INTERNAL, f"미구현: TODO({owner})")


class AwsProvider:
    """AWS 구현의 단일 진입점. 세부 로직은 deploy/health/tls 모듈에 위임한다."""

    name: ClassVar[str] = ProviderName.AWS.value
    target: ClassVar[Target] = Target.CLOUD

    def deploy(self, tier: str, ctx: RunContext) -> ProviderResult:
        raise _todo("C2")

    def rollback(self, tier: str, ctx: RunContext) -> ProviderResult:
        raise _todo("C2")

    def health_check(self, ctx: RunContext) -> ProviderResult:
        return check_cloud_health(ctx)

    def migrate_db(self, migrations: Sequence[str], ctx: RunContext) -> ProviderResult:
        raise _todo("C2")

    def inject_config(self, keys: Sequence[str], ctx: RunContext) -> ProviderResult:
        raise _todo("C2")

    def ensure_tls(self, mode: Literal["check", "apply"], ctx: RunContext) -> ProviderResult:
        raise _todo("C1")


__all__ = ["AwsProvider"]
