"""generate_plan 툴 등록(얇은 래퍼)."""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.generate_plan import GeneratePlanInput, GeneratePlanOutput
from ddak.core.registry import tool
from ddak.plan.planner import generate_plan as _generate


@tool("generate_plan")
def generate_plan(inp: GeneratePlanInput, ctx: RunContext) -> GeneratePlanOutput:
    """OPTIONAL step 포함 여부 초안(Jev -> Claude -> 규칙)."""
    return _generate(inp, ctx)
