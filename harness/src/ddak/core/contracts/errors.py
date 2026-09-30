"""툴 실패 코드와 예외.

실패 보고 규약(docs/harness/02 C-4):
- 작업을 수행할 수 없음 -> raise DdakToolError(code, msg) -> 실행기가 StepFailed(code)로 기록
- 검사 결과 불합격 -> 정상 반환 + passed=False (예외 아님) -> 실행기가 불합격으로 기록
- 예상 못한 예외 -> 실행기가 INTERNAL로 기록(메시지는 redact 후 저장)

메시지에 비밀값, 절대 경로, 계정 ID를 넣지 않는다(관리 페이지와 LLM으로 흘러간다).
"""

from __future__ import annotations

import re
from enum import StrEnum


class ErrorCode(StrEnum):
    """💭 초기안. 확정은 계약 문서(TODO(contract))."""

    PLAN_INVALID = "PLAN_INVALID"
    LOCK_HELD = "LOCK_HELD"
    LOCK_INVALID = "LOCK_INVALID"
    CONFIG_INVALID = "CONFIG_INVALID"
    INFRA_MISSING = "INFRA_MISSING"
    ADAPTER_TIMEOUT = "ADAPTER_TIMEOUT"
    ADAPTER_FAILED = "ADAPTER_FAILED"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"  # 예: 로컬 검증(local_verified) 미통과
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    TOGGLE_OFF = "TOGGLE_OFF"
    AI_NOT_ALLOWED = "AI_NOT_ALLOWED"
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    AI_OUTPUT_INVALID = "AI_OUTPUT_INVALID"
    INTERNAL = "INTERNAL"


class DdakToolError(Exception):
    """툴이 작업을 수행할 수 없을 때 던진다. 문자열 형식: "<CODE>: <메시지>"."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message


_CODE_IN_TEXT = re.compile(r"\b(" + "|".join(c.value for c in ErrorCode) + r"): ")


def parse_error_text(text: str) -> tuple[ErrorCode | None, str]:
    """`<CODE>: 메시지` 형식의 텍스트에서 코드를 꺼낸다(로그·이벤트 재생용)."""
    match = _CODE_IN_TEXT.search(text)
    if not match:
        return None, text
    return ErrorCode(match.group(1)), text[match.end() :]
