"""ping: 예시 툴(40개 밖). 새 툴의 입출력 모델은 이 파일을 복사해서 시작한다."""

from __future__ import annotations

from ddak.core.contracts.base import ContractModel, RunId, ToolInput
from ddak.core.contracts.enums import Target


class PingInput(ToolInput):
    """run_id + target. target은 실행기가 plan.json 트랙에서 채운다(AI가 고르지 않는다)."""

    target: Target


class PingOutput(ContractModel):
    """어느 어댑터가 응답했는지 돌려준다."""

    run_id: RunId
    target: Target
    adapter: str  # local | cloud | fake
    ok: bool
