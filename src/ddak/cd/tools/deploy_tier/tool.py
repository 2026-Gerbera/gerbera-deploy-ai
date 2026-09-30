"""O1 deploy_tier 공통 dispatch."""

from ddak.cd.dispatch import provider_for
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.deploy_tier import DeployTierInput, DeployTierOutput
from ddak.core.registry import tool


@tool("deploy_tier")
def deploy_tier(inp: DeployTierInput, ctx: RunContext) -> DeployTierOutput:
    result = provider_for(inp, ctx).deploy(inp.tier, ctx)
    return DeployTierOutput.model_validate(result.model_dump(mode="json"))
