"""공통 CD 경로에서 조립부가 주입한 C1 검사 함수를 호출한다."""

from ddak.cd.dispatch import check_tls_for
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.tools.ensure_tls import EnsureTlsInput, EnsureTlsOutput
from ddak.core.registry import tool


@tool("ensure_tls")
def ensure_tls(inp: EnsureTlsInput, ctx: RunContext) -> EnsureTlsOutput:
    result = check_tls_for(inp, inp.mode, ctx)
    return EnsureTlsOutput(
        **result.model_dump(mode="json"),
        source=Source.FIXTURE if ctx.adapter_mode is AdapterMode.FAKE else Source.LIVE,
    )
