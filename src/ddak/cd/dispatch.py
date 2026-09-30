"""CD 툴 공통 연결. provider 선택은 코드의 target/mode로만 한다(AI가 고르지 않는다).

provider 구현 위치(환경별 팀 디렉토리):
- cloud(AWS): ddak.cloud.deploy (AwsProvider)
- local(온프렘): ddak.onprem.deploy (OnPremProvider)
- 테스트·드라이런: ddak.cd.fake (FakeProvider)
각 패키지의 __init__.py가 노출하는 공개 이름만 import한다.
"""

from __future__ import annotations

from ddak.cd.fake import FakeProvider
from ddak.cd.interface import CdProvider
from ddak.cloud.deploy import AwsProvider
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.cd import CdInput
from ddak.onprem.deploy import OnPremProvider


def select_provider(target: Target, mode: AdapterMode) -> CdProvider:
    if mode is AdapterMode.FAKE:
        return FakeProvider(target)
    return OnPremProvider() if target is Target.LOCAL else AwsProvider()


def provider_for(inp: CdInput, ctx: RunContext) -> CdProvider:
    if inp.run_id != ctx.run_id or not ctx.lock_token or inp.lock_token != ctx.lock_token:
        raise DdakToolError(ErrorCode.LOCK_INVALID, "실행 잠금이 일치하지 않는다")
    return select_provider(inp.target, ctx.adapter_mode)


__all__ = ["AwsProvider", "FakeProvider", "OnPremProvider", "provider_for", "select_provider"]
