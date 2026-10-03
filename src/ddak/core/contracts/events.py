"""진행 이벤트(stream_progress). 실행기 -> EventBus -> JSONL(var/runs/<run_id>/events.jsonl) -> SSE.

💭 초안(설계 문서 02 파이프라인과 계획 8-7). 필드 확정은 계약 문서(TODO(contract)).
- JSONL에 먼저 쓰고 SSE로 내보낸다. SSE가 끊기면 run_id로 JSONL을 다시 읽어 재생한다.
- 모든 문자열은 redact를 거친다(EventBus.publish가 detail을 다시 가린다).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from ddak.core.contracts.base import ContractModel, RunId
from ddak.core.contracts.enums import Stage, Target


class EventType(StrEnum):
    RUN_STATE = "run.state"
    STEP_STARTED = "step.started"
    STEP_FINISHED = "step.finished"
    STEP_SKIPPED = "step.skipped"
    GATE_WAITING = "gate.waiting"
    GATE_OPENED = "gate.opened"
    GATE_FAILED = "gate.failed"
    ROLLBACK_STARTED = "rollback.started"
    ROLLBACK_FINISHED = "rollback.finished"
    AI_CALL = "ai.call"
    REPORT_READY = "report.ready"


class RunEvent(ContractModel):
    """관리 페이지 2열 진행 화면이 읽는 이벤트 한 줄."""

    run_id: RunId
    seq: int = Field(ge=0)
    type: EventType
    ts: str | None = None  # ISO 8601(UTC)
    stage: Stage | None = None
    step: str | None = None  # step id, 예) deploy.migrate.cloud
    tool: str | None = None
    target: Target | None = None
    status: str | None = None  # succeeded | failed | check_failed | skipped | waiting | ...
    elapsed_s: float | None = None
    detail: str | None = Field(default=None, max_length=4096)  # 표시용(redact 후)
