"""ping: 예시 툴. 새 툴은 이 디렉토리를 복사해서 시작한다(docs/harness/02 C-1).

tool.py에는 로직을 두지 않는다: 입력 모델 -> (어댑터 선택) -> 호출 -> 출력 모델 반환.
- 등록: @tool("<카탈로그 이름>"). 이름·위치(모듈)·시그니처가 카탈로그와 다르면 앱 기동이 실패한다.
- 시그니처: def <tool>(inp: <Tool>Input, ctx: RunContext) -> <Tool>Output
- 어댑터: target과 ctx.adapter_mode로 코드가 고른다(AI가 고르지 않는다).
- blocking 호출(boto3, docker)은 동기 함수로 둔다. 실행기가 워커 스레드에서 돌린다.
"""

from __future__ import annotations

from ddak.core.adapters import AdapterSet, select_adapter
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.ping import PingInput, PingOutput
from ddak.core.registry import PING, tool
from ddak.core.tools.ping.cloud import CloudPingAdapter
from ddak.core.tools.ping.fake import FakePingAdapter
from ddak.core.tools.ping.local import LocalPingAdapter
from ddak.core.tools.ping.logic import PingAdapter

ADAPTERS: AdapterSet[PingAdapter] = AdapterSet(
    local=LocalPingAdapter, cloud=CloudPingAdapter, fake=FakePingAdapter
)


@tool(PING)
def ping(inp: PingInput, ctx: RunContext) -> PingOutput:
    """대상 환경 어댑터가 응답하는지 확인한다."""
    adapter = select_adapter(ADAPTERS, inp.target, ctx.deploy_config, ctx.adapter_mode)
    return PingOutput(run_id=inp.run_id, target=inp.target, adapter=adapter.name, ok=adapter.ping())
