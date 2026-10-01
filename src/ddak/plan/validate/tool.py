"""validate_plan 툴 등록(얇은 래퍼). 로직은 assemble.py·rules.py."""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.validate_plan import ValidatePlanInput, ValidatePlanOutput
from ddak.core.registry import tool
from ddak.plan.validate.assemble import assemble


@tool("validate_plan")
def validate_plan_tool(inp: ValidatePlanInput, ctx: RunContext) -> ValidatePlanOutput:
    """초안(없으면 규칙 계획)을 카탈로그 기준으로 검증·조립한다."""
    return assemble(inp, ctx)
