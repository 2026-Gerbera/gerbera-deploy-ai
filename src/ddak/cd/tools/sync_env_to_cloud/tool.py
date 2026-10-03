"""Terraform이 만든 빈 Secrets Manager 시크릿에 런타임 값을 채운다."""

from ddak.cd.dispatch import provider_for
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.inject_env_config import InjectEnvConfigInput, InjectEnvConfigOutput
from ddak.core.registry import tool


@tool("sync_env_to_cloud")
def sync_env_to_cloud(inp: InjectEnvConfigInput, ctx: RunContext) -> InjectEnvConfigOutput:
    result = provider_for(inp, ctx).inject_config(inp.keys, ctx)
    return InjectEnvConfigOutput.model_validate(result.model_dump(mode="json"))
