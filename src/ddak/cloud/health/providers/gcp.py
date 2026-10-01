"""GCP health/TLS 검증 provider 자리."""

from ddak.cd.interface import ProviderResult
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.verify_tls import VerifyTlsInput, VerifyTlsOutput


def _unsupported(function: str) -> DdakToolError:
    return DdakToolError(
        ErrorCode.CONFIG_INVALID,
        f"gcp provider의 {function} 기능은 아직 지원하지 않는다",
    )


def health_check(ctx: RunContext) -> ProviderResult:
    raise _unsupported("health_check")


def verify_tls(inp: VerifyTlsInput, ctx: RunContext) -> VerifyTlsOutput:
    raise _unsupported("verify_tls")


__all__ = ["health_check", "verify_tls"]
