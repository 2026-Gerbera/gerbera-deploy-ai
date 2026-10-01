"""대상별 provider의 health_check를 호출하는 공통 dispatch."""

from ddak.cd.dispatch import provider_for
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.health_check import HealthCheckInput, HealthCheckOutput
from ddak.core.registry import tool


@tool("health_check")
def health_check(inp: HealthCheckInput, ctx: RunContext) -> HealthCheckOutput:
    result = provider_for(inp, ctx).health_check(ctx)
    return HealthCheckOutput.model_validate(result.model_dump(mode="json"))
