from ddak.cloud.infra import run_apply
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.apply_infra import ApplyInfraInput, ApplyInfraOutput
from ddak.core.registry import tool


@tool("apply_infra")
def apply_infra(inp: ApplyInfraInput, ctx: RunContext) -> ApplyInfraOutput:
    return run_apply(inp, ctx)
