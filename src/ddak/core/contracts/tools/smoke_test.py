"""smoke_test 입출력(C-10 스모크 결과 형식 초안, 담당 O3).

배포한 앱에 같은 시나리오를 환경마다 독립적으로 보내고 결과를 남긴다.
- 대상 주소는 입력이 아니라 RunContext(온프렘 인벤토리 / cloud_domain)에서 읽는다.
- 결과에는 쿠키 값·비밀값·주소를 넣지 않는다. 쿠키는 이름과 속성만 남긴다.
- 실행기는 passed만 판정에 쓰고, compare_env_results가 scenarios[].normalized를 비교한다.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ddak.core.contracts.base import ContractModel, RunId, ToolInput
from ddak.core.contracts.enums import Target

ScenarioGroup = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
ScenarioId = Annotated[str, Field(pattern=r"^[A-Z][A-Za-z0-9_.-]{0,31}$")]
# 비교용 관찰값. 값은 짧은 스칼라만(본문·헤더 원문을 넣지 않는다)
Observed = str | int | bool | None


class SmokeTestInput(ToolInput):
    """target은 실행기가 트랙에서 채운다. scenarios는 plan step params(시나리오 묶음 이름)."""

    target: Target
    scenarios: list[ScenarioGroup] = Field(default_factory=lambda: ["base"], max_length=8)


class SmokeScenario(ContractModel):
    id: ScenarioId
    ok: bool
    status: int | None = Field(default=None, ge=100, le=599)  # HTTP 상태(연결 실패면 None)
    detail: str = Field(default="", max_length=200)  # 실패 이유(redact된 짧은 문장)
    normalized: dict[str, Observed] = Field(default_factory=dict)


class SmokeTestOutput(ContractModel):
    run_id: RunId
    target: Target
    passed: bool
    elapsed_s: float = Field(ge=0)
    scenarios: list[SmokeScenario]
    source: Literal["live", "fixture"] = "live"  # fake 어댑터 결과는 fixture
