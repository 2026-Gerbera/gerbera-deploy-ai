"""verify_tls 레지스트리 연결."""

from ddak.cloud.health.fake import fake_verify_tls
from ddak.cloud.health.tls import verify_tls as verify_tls_real
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.verify_tls import VerifyTlsInput, VerifyTlsOutput
from ddak.core.registry import tool


@tool("verify_tls")
def verify_tls(inp: VerifyTlsInput, ctx: RunContext) -> VerifyTlsOutput:
    if ctx.adapter_mode is AdapterMode.FAKE:
        return fake_verify_tls(inp.run_id)
    return verify_tls_real(inp, ctx)
