"""generate_infra 레지스트리 연결."""

from ddak.cloud.infra.tools.generate_infra import generate_infra as _generate
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.generate_infra import GenerateInfraInput, GenerateInfraOutput
from ddak.core.registry import tool


@tool("generate_infra")
def generate_infra(inp: GenerateInfraInput, ctx: RunContext) -> GenerateInfraOutput:
    return _generate(inp, ctx)
