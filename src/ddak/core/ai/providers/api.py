"""공통 API 인터페이스와 legacy API=Groq migration alias.

Deprecated: AnthropicApiProvider는 과거 API backend=Groq의 잘못된 이름이다.
새 코드는 groq_api.GroqApiProvider 또는 anthropic_api.ClaudeApiProvider를 사용한다.
실제 Claude 구현으로 alias를 바꾸면 legacy 키가 다른 서비스로 가므로 하지 않는다.
"""

import urllib.request  # legacy fakeHTTP monkeypatch 경로 호환

from ddak.core.ai.providers.groq_api import GROQ_ENDPOINT, GroqApiProvider, groq_chat
from ddak.core.ai.providers.types import AIRequest, AIResponse, LLMProvider

AnthropicApiProvider = GroqApiProvider  # deprecated compatibility alias (실제로 Groq)

__all__ = [
    "GROQ_ENDPOINT",
    "AIRequest",
    "AIResponse",
    "AnthropicApiProvider",
    "GroqApiProvider",
    "LLMProvider",
    "groq_chat",
    "urllib",
]
