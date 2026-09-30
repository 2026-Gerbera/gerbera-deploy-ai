"""cloud/deploy/database.py: RDS 마이그레이션(ECS 일회성 태스크). 담당 안승환(C2).

추가형 마이그레이션만. 결과는 온프렘과 같은 migration 모양(ProviderResult.migration).
AI import 금지.
"""

from __future__ import annotations

from collections.abc import Sequence

from ddak.cd.interface import ProviderResult
from ddak.core.contracts.context import RunContext

_TODO = "cloud/deploy 미구현: 담당 안승환"


def run_migrations(migrations: Sequence[str], ctx: RunContext) -> ProviderResult:
    """migrations를 순서대로 적용·확인한다. 출력: ProviderResult(function="migrate_db")."""
    raise NotImplementedError(_TODO)
