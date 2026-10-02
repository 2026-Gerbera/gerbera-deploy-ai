"""detect_changed_tiers 툴 등록(얇은 래퍼)."""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.detect_changed_tiers import (
    DetectChangedTiersInput,
    DetectChangedTiersOutput,
)
from ddak.core.registry import tool
from ddak.plan.detect import detect_changed_tiers as _detect


@tool("detect_changed_tiers")
def detect_changed_tiers(inp: DetectChangedTiersInput, ctx: RunContext) -> DetectChangedTiersOutput:
    """환경별 마지막 성공 배포와 비교해 바뀐 tier·새 마이그레이션·facts 해시를 돌려준다."""
    return _detect(inp, ctx)
