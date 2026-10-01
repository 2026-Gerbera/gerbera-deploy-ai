"""아직 구현되지 않은 CSP provider의 안전한 공통 동작."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar, Literal

from ddak.cd.interface import ProviderResult
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode


class UnsupportedCloudProvider:
    """지원 전 CSP가 AWS 코드로 잘못 배포되는 것을 막는다."""

    name: ClassVar[str]
    target: ClassVar[Target] = Target.CLOUD

    def _unsupported(self, function: str) -> DdakToolError:
        return DdakToolError(
            ErrorCode.CONFIG_INVALID,
            f"{self.name} provider의 {function} 기능은 아직 지원하지 않는다",
        )

    def deploy(self, tier: str, ctx: RunContext) -> ProviderResult:
        raise self._unsupported("deploy")

    def rollback(self, tier: str, ctx: RunContext) -> ProviderResult:
        raise self._unsupported("rollback")

    def health_check(self, ctx: RunContext) -> ProviderResult:
        raise self._unsupported("health_check")

    def migrate_db(self, migrations: Sequence[str], ctx: RunContext) -> ProviderResult:
        raise self._unsupported("migrate_db")

    def inject_config(self, keys: Sequence[str], ctx: RunContext) -> ProviderResult:
        raise self._unsupported("inject_config")

    def ensure_tls(self, mode: Literal["check", "apply"], ctx: RunContext) -> ProviderResult:
        raise self._unsupported("ensure_tls")


__all__ = ["UnsupportedCloudProvider"]
