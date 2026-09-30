"""O1 inject_env_config 공통 dispatch."""

from ddak.cd.dispatch import provider_for
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.inject_env_config import InjectEnvConfigInput, InjectEnvConfigOutput
from ddak.core.registry import tool


@tool("inject_env_config")
def inject_env_config(inp: InjectEnvConfigInput, ctx: RunContext) -> InjectEnvConfigOutput:
    result = provider_for(inp, ctx).inject_config(inp.keys, ctx)
    return InjectEnvConfigOutput.model_validate(result.model_dump(mode="json"))
