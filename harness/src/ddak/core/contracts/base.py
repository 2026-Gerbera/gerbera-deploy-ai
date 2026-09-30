"""모든 계약 모델의 기반 클래스와 공용 필드 타입."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# 툴 함수 규약(docs/harness/02): def <tool>(inp: <Tool>Input, ctx: RunContext) -> <Tool>Output
# 실행기가 plan.json step으로 입력 모델을 만들어 레지스트리 함수를 직접 호출한다.
# (MCP 호출 규약 {"payload": ...}는 없어졌다.)

# 💭 run_id 형식은 계약 문서가 확정한다(TODO(contract)). 경로에 쓰이므로 안전한 문자만 허용한다.
RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
# tier 이름은 프로젝트마다 달라 정적 enum으로 두지 않는다. 실행 시 deploy.yaml과 대조한다.
TIER_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"

RunId = Annotated[str, Field(pattern=RUN_ID_PATTERN, description="실행 ID. 모든 툴의 필수 입력")]
TierName = Annotated[str, Field(pattern=TIER_PATTERN, description="deploy.yaml에 정의된 tier")]
LockToken = Annotated[
    str, Field(min_length=8, max_length=128, description="acquire_deploy_lock이 발급한 토큰")
]


class ContractModel(BaseModel):
    """계약 모델 기반. 모르는 필드는 거부하고, 만든 뒤에는 바꿀 수 없다."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolInput(ContractModel):
    """모든 툴 입력의 기반. run_id를 항상 인자로 받는다(툴은 상태를 들고 있지 않는다)."""

    run_id: RunId


class AIUsage(ContractModel):
    """AI 툴 출력에 붙이는 사용량. 💭 필드 확정은 계약 문서(TODO(contract))."""

    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    latency_ms: int = Field(ge=0)
