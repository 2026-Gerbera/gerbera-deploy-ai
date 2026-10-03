"""LLM·Jev provider. anthropic 등 SDK는 이 패키지에서만 import한다(ruff TID251 예외).

backend(✅ 장부 7):
- cli: 개발. 운영자 본인 로컬에 로그인해 둔 Claude CLI(`claude -p`, 도구 전부 끔, 인자 배열).
       구독 CLI는 운영자 본인이 로컬에서만 쓴다. 관리 페이지를 외부에 열어 타인이 채팅하거나,
       구독 토큰을 서버·ECS·컨테이너에 넣거나, 웹에서 Claude 로그인을 받는 것은 금지 패턴이다.
- api: 데모(외부 공개 포함). Anthropic API 키.
- replay: 테스트·비상. 저장된 응답. 화면에 "저장된 응답" 라벨(source=replay).
가림·타임아웃·재시도·스키마 검증·비용 기록은 gateway.call_ai가 공통으로 한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ddak.core.config import Settings
from ddak.core.contracts.base import AIUsage
from ddak.core.contracts.enums import LLMBackend, Source


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


def get_provider(settings: Settings) -> LLMProvider:
    """설정의 backend로 provider를 만든다. 어댑터처럼 코드가 고르고 AI가 고르지 않는다."""
    if settings.llm_backend is LLMBackend.CLI:
        from ddak.core.ai.providers.cli import ClaudeCliProvider

        return ClaudeCliProvider(settings.claude_bin, effort=settings.llm_effort)
    if settings.llm_backend is LLMBackend.API:
        from ddak.core.ai.providers.api import AnthropicApiProvider

        return AnthropicApiProvider(settings.llm_api_key)
    from ddak.core.ai.providers.replay import ReplayProvider

    return ReplayProvider(settings.ai_replay_dir)
