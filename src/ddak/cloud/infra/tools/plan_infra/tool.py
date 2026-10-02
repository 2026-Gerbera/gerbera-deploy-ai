from ddak.cloud.infra import run_plan
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.plan_infra import PlanInfraInput, PlanInfraOutput
from ddak.core.registry import tool


@tool("plan_infra")
def plan_infra(inp: PlanInfraInput, ctx: RunContext) -> PlanInfraOutput:
    return run_plan(inp, ctx)
