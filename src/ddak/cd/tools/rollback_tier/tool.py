"""O1 rollback_tier 공통 dispatch."""

from ddak.cd.dispatch import provider_for
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.rollback_tier import RollbackTierInput, RollbackTierOutput
from ddak.core.registry import tool


@tool("rollback_tier")
def rollback_tier(inp: RollbackTierInput, ctx: RunContext) -> RollbackTierOutput:
    result = provider_for(inp, ctx).rollback(inp.tier, ctx)
    return RollbackTierOutput.model_validate(result.model_dump(mode="json"))
