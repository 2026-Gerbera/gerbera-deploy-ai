"""plan/validate: 계획 검증·조립(결정적, AI 금지). 담당 정준우(O1, O2 승계).

공개 함수: validate_plan. ddak.core.ai·plan/planner·plan/dockerfile 생성기를 import하지 않는다
(import-linter 계약 5). 툴 등록은 tool.py.
"""

from __future__ import annotations

from collections.abc import Collection

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.plan import Plan
from ddak.core.contracts.tools.validate_plan import ValidatePlanInput
from ddak.plan.validate.assemble import assemble

__all__ = ["validate_plan"]


def validate_plan(
    inp: ValidatePlanInput, ctx: RunContext, *, registered_tools: Collection[str] | None = None
) -> Plan:
    """draft=None이면 규칙 계획. 등록 집합 생략 시 호출 시점의 실제 레지스트리를 쓴다."""
    return assemble(inp, ctx, registered_tools=registered_tools)
