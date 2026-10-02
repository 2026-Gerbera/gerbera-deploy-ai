"""CD 툴 공통 연결. provider 선택은 코드의 target/mode로만 한다(AI가 고르지 않는다).

provider 구현 위치(환경별 팀 디렉토리):
- cloud(AWS): ddak.cloud.deploy (AwsProvider)
- local(온프렘): ddak.onprem.deploy (OnPremProvider)
- 테스트·드라이런: ddak.cd.fake (FakeProvider)
각 패키지의 __init__.py가 노출하는 공개 이름만 import한다.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from ddak.cd.fake import FakeProvider
from ddak.cd.interface import CdProvider, ProviderResult
from ddak.cloud.deploy import AwsProvider
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.cd import CdInput
from ddak.onprem.deploy import OnPremProvider

TlsChecker = Callable[[Literal["check", "apply"], RunContext], ProviderResult]
_cloud_tls: TlsChecker | None = None


def configure_cloud_tls(checker: TlsChecker) -> None:
    """조립부가 C1 공개 함수를 주입한다. C2 provider 구현은 수정하지 않는다."""
    global _cloud_tls
    _cloud_tls = checker


def check_tls_for(inp: CdInput, mode: Literal["check", "apply"], ctx: RunContext) -> ProviderResult:
    provider = provider_for(inp, ctx)
    if mode != "check":
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "TLS 변경은 플랫폼 Terraform만 수행한다")
    if inp.target is not Target.CLOUD or ctx.adapter_mode is AdapterMode.FAKE:
        return provider.ensure_tls(mode, ctx)
    if _cloud_tls is None:
        raise DdakToolError(ErrorCode.INFRA_MISSING, "클라우드 TLS 검사 연결이 필요하다")
    return _cloud_tls(mode, ctx)


def select_provider(target: Target, mode: AdapterMode) -> CdProvider:
    if mode is AdapterMode.FAKE:
        return FakeProvider(target)
    return OnPremProvider() if target is Target.LOCAL else AwsProvider()


def provider_for(inp: CdInput, ctx: RunContext) -> CdProvider:
    if inp.run_id != ctx.run_id or not ctx.lock_token or inp.lock_token != ctx.lock_token:
        raise DdakToolError(ErrorCode.LOCK_INVALID, "실행 잠금이 일치하지 않는다")
    return select_provider(inp.target, ctx.adapter_mode)


__all__ = ["AwsProvider", "FakeProvider", "OnPremProvider", "provider_for", "select_provider"]
