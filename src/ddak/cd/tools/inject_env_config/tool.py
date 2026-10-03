"""O1 inject_env_config 공통 dispatch."""

from ddak.cd.dispatch import provider_for
from ddak.cd.interface import ProviderResult
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.inject_env_config import InjectEnvConfigInput, InjectEnvConfigOutput
from ddak.core.registry import tool


@tool("inject_env_config")
def inject_env_config(inp: InjectEnvConfigInput, ctx: RunContext) -> InjectEnvConfigOutput:
    result = (
        ProviderResult(
            provider="aws",
            function="inject_config",
            changed=False,
            keys=inp.keys,
            detail="ECS 새 태스크 리비전에 일반 설정을 결합할 준비 완료",
        )
        if inp.target.value == "cloud" and ctx.adapter_mode is AdapterMode.REAL
        else provider_for(inp, ctx).inject_config(inp.keys, ctx)
    )
    return InjectEnvConfigOutput.model_validate(result.model_dump(mode="json"))
