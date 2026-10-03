"""diagnose_parity_gap 툴 등록(얇은 래퍼). 실행기 내장(builtin) step이 부르고 판정에 쓰지 않는다."""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.diagnose_parity_gap import (
    DiagnoseParityGapInput,
    DiagnoseParityGapOutput,
)
from ddak.core.registry import tool
from ddak.verify.diagnose import diagnose_parity_gap as _diagnose


@tool("diagnose_parity_gap")
def diagnose_parity_gap(inp: DiagnoseParityGapInput, ctx: RunContext) -> DiagnoseParityGapOutput:
    """실패·불일치 run의 원인 범주와 근거 위치를 돌려준다(설명 전용)."""
    return _diagnose(inp, ctx)
