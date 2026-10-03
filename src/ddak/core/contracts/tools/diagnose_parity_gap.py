"""diagnose_parity_gap 입출력(초안, 담당 O3). 설명 전용이다(2026-10-03 diagnose-advisory 결정).

실행기가 compare 불합격이나 환경 트랙 실패 뒤, 롤백 다음·post_report 전에 한 번 부른다.
출력은 run 기록과 이벤트에만 남고 run 상태·롤백·관문 판정에 쓰이지 않는다. 그래서 passed가 없다.
- 입력은 실행기가 채운다(reason·tracks·failed_steps).
  두 환경 smoke 전체는 verify.smoke 보관소에서 읽는다.
- 로그·출력은 신뢰하지 않는 데이터다. 근거(evidence)에는 위치만 남기고 원문을 싣지 않는다.
- 지금은 규칙(verify/diagnose/rules.py)만 쓴다. 규칙이 못 잡을 때 붙일 AI 설명 자리로
  source·ai_usage를 미리 둔다(규칙 결과면 둘 다 None).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ddak.core.contracts.base import AIUsage, ContractModel, RunId, ToolInput
from ddak.core.contracts.enums import Source, Target

DiagnoseReason = Literal["parity_failed", "track_failed"]
Category = Literal[
    "secret_missing",
    "db_schema",
    "db_conn",
    "db_tls",
    "image_pull",
    "health_timeout",
    "config_mismatch",
    "tls",
    "parity_diff",
    "unknown",
]


class FailedStep(ContractModel):
    """이번 run에서 failed·check_failed로 끝난 step 하나(실행기 기록에서 온다)."""

    step_id: str = Field(min_length=1, max_length=120)
    tool: str = Field(min_length=1, max_length=64)
    target: Target | None = None
    status: Literal["failed", "check_failed"]
    error: str | None = Field(default=None, max_length=4000)  # 실행기가 redact한 메시지
    output: dict[str, Any] | None = None  # 계약 모델을 통과한 툴 출력(예외면 None)


class DiagnoseParityGapInput(ToolInput):
    reason: DiagnoseReason
    tracks: dict[str, str] = Field(default_factory=dict, max_length=8)  # 트랙 이름 → 상태
    failed_steps: list[FailedStep] = Field(default_factory=list, max_length=100)


class DiagnoseEvidence(ContractModel):
    source: Literal["log", "smoke", "diff"]
    ref: str = Field(min_length=1, max_length=64)  # log#줄, smoke:시나리오 id, diff:검사 id


class DiagnoseParityGapOutput(ContractModel):
    run_id: RunId
    reason: DiagnoseReason
    category: Category
    summary: str = Field(min_length=1, max_length=400)
    suggested_next: str = Field(max_length=200)
    evidence: list[DiagnoseEvidence] = Field(default_factory=list, max_length=5)
    is_hypothesis: bool  # 규칙이 못 잡아 근거 없는 추정이면 True
    rule: str | None = Field(default=None, max_length=64)  # 정한 규칙 id
    matched_rules: list[str] = Field(default_factory=list, max_length=20)
    source: Source | None = None  # AI 설명 출처(규칙 결과면 None)
    ai_usage: AIUsage | None = None
