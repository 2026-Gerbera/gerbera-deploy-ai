"""api backend(데모): Anthropic Python SDK로 Messages API를 부른다. 툴 없이 JSON 스키마 출력만.

확인 필요:
- anthropic SDK는 아직 의존성에 없다. O2가 `uv add anthropic`으로 넣는다(버전은 설치 시 확인).
  1.x는 httpx2 기반이다. 없으면 AI_UNAVAILABLE로 물러난다.
- 구조화 출력(output_config.format json_schema)이 지원하는 JSON Schema 범위와 pydantic
  model_json_schema() 결과가 맞는지 `make test-llm`으로 확인한다.
- 모델 ID는 DDAK_LLM_MODEL로 고정한다(예: claude-sonnet-5-5 / claude-opus-5-5, 실측으로 선택).
  날짜 접미사를 붙이지 않는다. Sonnet 5.5·Opus 5.5는 temperature 등 sampling 파라미터를 거부한다.
- 서버 측 refusal fallback(`fallbacks`)은 켜지 않았다. 거절이면 AI_UNAVAILABLE -> 호출한 툴이
  규칙 대체 경로로 간다(모델 ID 고정·결정성 우선). 켤지는 O2가 정한다.
"""

from __future__ import annotations

import time
from typing import Any

from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.contracts.base import AIUsage
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode

# 출력은 작은 JSON이다. 잘리면 stop_reason=max_tokens -> 스키마 파싱 실패로 드러난다.
MAX_TOKENS = 8192


class AnthropicApiProvider:
    name = "api"

    def __init__(self, api_key: str | None, *, max_tokens: int = MAX_TOKENS) -> None:
        self._key = api_key
        self._max_tokens = max_tokens

    def complete(self, req: AIRequest) -> AIResponse:
        if not self._key:
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "DDAK_LLM_API_KEY가 없다")
        if not req.model:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "DDAK_LLM_MODEL이 비어 있다")
        try:
            # anthropic은 아직 의존성에 없다. O2가 `uv add anthropic` 한 뒤 아래 ignore를 지운다.
            import anthropic  # pyright: ignore[reportMissingImports]
        except ImportError as exc:
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "anthropic SDK 미설치") from exc

        # SDK 기본값(timeout 10분, max_retries 2)을 쓰지 않는다. 재시도는 call_ai가 한다.
        client = anthropic.Anthropic(api_key=self._key, timeout=req.timeout_s, max_retries=0)
        started = time.monotonic()
        try:
            response: Any = client.messages.create(
                model=req.model,
                max_tokens=self._max_tokens,
                system=req.system,
                messages=[{"role": "user", "content": req.user}],
                output_config={"format": {"type": "json_schema", "schema": dict(req.json_schema)}},
            )
        except anthropic.APITimeoutError as exc:
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "API 타임아웃") from exc
        except anthropic.RateLimitError as exc:
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "API 속도 제한(429)") from exc
        except anthropic.APIStatusError as exc:
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, f"API 오류 {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "API 연결 실패") from exc

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, f"모델이 거절했다(category={category})")
        text = next((b.text for b in response.content if b.type == "text"), "")
        usage = AIUsage(
            model=req.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=0.0,  # TODO(O2): 모델별 단가표로 계산해 결과 카드에 표시
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return AIResponse(text=text, source=Source.LIVE, usage=usage)
