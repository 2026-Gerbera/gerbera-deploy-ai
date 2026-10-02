from ddak.cloud.infra import run_validate
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.validate_infra import ValidateInfraInput, ValidateInfraOutput
from ddak.core.registry import tool


@tool("validate_infra")
def validate_infra(inp: ValidateInfraInput, ctx: RunContext) -> ValidateInfraOutput:
    return run_validate(inp, ctx)
