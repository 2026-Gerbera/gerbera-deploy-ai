"""plan/validate: 계획 검증·조립(결정적, AI 금지). 담당 김준석(O2).

공개 함수: validate_plan. ddak.core.ai·plan/planner·plan/dockerfile 생성기를 import하지 않는다
(import-linter 계약 5). 툴 등록은 tool.py.
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.plan import Plan
from ddak.core.contracts.tools.validate_plan import ValidatePlanInput
from ddak.plan.validate.assemble import assemble

__all__ = ["validate_plan"]


def validate_plan(inp: ValidatePlanInput, ctx: RunContext) -> Plan:
    """순수 함수. draft=None이면 규칙 계획. 구조 위반은 DdakToolError(PLAN_INVALID)."""
    return assemble(inp, ctx)
