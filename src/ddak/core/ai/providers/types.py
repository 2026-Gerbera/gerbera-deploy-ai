"""생성 요청과 응답 계약. provider 선택과 분리한다."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ddak.core.contracts.base import AIUsage
from ddak.core.contracts.enums import Source


@dataclass(frozen=True)
class AIRequest:
    purpose: str  # 부른 툴 이름(= replay 디렉토리 이름)
    system: str
    user: str  # redact와 untrusted_data 감싸기를 거친 뒤의 문자열
    json_schema: Mapping[str, Any] = field(default_factory=dict)
    model: str | None = None
    timeout_s: float = 20.0
    prompt_version: str = "v0"


@dataclass(frozen=True)
class AIResponse:
    text: str  # JSON 문자열(출력 스키마)
    source: Source
    usage: AIUsage | None = None


class LLMProvider(Protocol):
    name: str

    def complete(self, req: AIRequest) -> AIResponse:
        """JSON 문자열을 돌려준다. 수행 불가면 DdakToolError(AI_UNAVAILABLE 등)."""
        ...
