"""Fake provider: 결정적 정상 결과(테스트·드라이런·UI 개발). 실제 환경을 건드리지 않는다."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Literal

from ddak.cd.interface import ProviderResult
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.env_keys import check_runtime_keys


@dataclass(frozen=True)
class FakeProvider:
    target: Target
    name: ClassVar[str] = "fake"

    def _ok(self, function: str, *, applicable: bool = True) -> ProviderResult:
        return ProviderResult(
            provider=f"fake-{self.target.value}",
            function=function,
            applicable=applicable,
        )

    def deploy(self, tier: str, ctx: RunContext) -> ProviderResult:
        return self._ok("deploy")

    def rollback(self, tier: str, ctx: RunContext) -> ProviderResult:
        return self._ok("rollback")

    def health_check(self, ctx: RunContext) -> ProviderResult:
        return self._ok("health_check")

    def migrate_db(self, migrations: Sequence[str], ctx: RunContext) -> ProviderResult:
        return self._ok("migrate_db")

    def inject_config(self, keys: Sequence[str], ctx: RunContext) -> ProviderResult:
        check_runtime_keys(keys)
        return self._ok("inject_config")

    def ensure_tls(self, mode: Literal["check", "apply"], ctx: RunContext) -> ProviderResult:
        return self._ok("ensure_tls", applicable=self.target is Target.CLOUD)
