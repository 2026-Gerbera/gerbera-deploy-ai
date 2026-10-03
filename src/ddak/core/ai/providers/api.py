"""Anthropic Messages API backend. Jev의 임시 Groq helper도 이 모듈에 남아 있다.

Claude 호출은 공식 SDK의 structured outputs(`output_config.format`)를 사용한다. SDK 자동
재시도는 끄고 공통 관문 `call_ai`가 횟수와 `retry-after`를 관리한다. 키·요청 본문·공급자
오류 본문은 로그나 제품 오류 메시지에 넣지 않는다.

Jev는 아직 Groq 임시 연결이므로 `groq_chat`을 공유한다. Claude와 Jev 키는 서로 다르다.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, cast

import anthropic
from anthropic.types import OutputConfigParam

from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.contracts.base import AIUsage
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode

# Terraform 초안처럼 큰 구조화 출력도 한 번에 받을 수 있어야 한다.
MAX_TOKENS = 32768
# Jev 임시 연결 전용.
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
_MAX_RATE_LIMIT_WAIT_S = 90.0
_CLAUDE_PRICES_PER_MILLION = {
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
}

# Claude structured outputs는 배열의 길이 제약을 입력 스키마에서 지원하지 않는다.
# Pydantic은 응답을 받은 뒤 원래 모델로 다시 검증하므로 여기서 제거해도 제품 계약의
# min_length/max_length 검증은 유지된다.
_UNSUPPORTED_CLAUDE_SCHEMA_KEYS = frozenset({"minItems", "maxItems"})


def _claude_json_schema(value: Any) -> Any:
    """Claude가 받지 않는 JSON Schema 키만 재귀적으로 제거한다."""
    if isinstance(value, Mapping):
        return {
            key: _claude_json_schema(item)
            for key, item in value.items()
            if key not in _UNSUPPORTED_CLAUDE_SCHEMA_KEYS
        }
    if isinstance(value, list):
        return [_claude_json_schema(item) for item in value]
    return value


class ApiRateLimitError(DdakToolError):
    def __init__(self, retry_after_s: float) -> None:
        super().__init__(ErrorCode.AI_UNAVAILABLE, "API 속도 제한(429)")
        self.retry_after_s = retry_after_s


def _retry_after(headers: Any) -> float:
    try:
        value = float(headers.get("retry-after", "5"))
    except (TypeError, ValueError):
        value = 5.0
    return min(max(value, 0.1), _MAX_RATE_LIMIT_WAIT_S)


def groq_chat(
    *,
    url: str,
    key: str,
    model: str,
    system: str,
    user: str,
    timeout_s: float,
    max_tokens: int,
    json_schema: Mapping[str, Any] | None = None,
) -> tuple[str, int, int]:
    """chat 1회 -> (text, input_tokens, output_tokens). 실패는 AI_UNAVAILABLE. jev.py도 쓴다."""
    response_format: dict[str, Any] = {"type": "json_object"}
    if json_schema:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "ddak_output",
                "strict": False,
                "schema": dict(json_schema),
            },
        }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": response_format,
    }
    http_req = urllib.request.Request(  # noqa: S310 - 고정 https 엔드포인트
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "ddak/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_req, timeout=timeout_s) as resp:  # noqa: S310
            data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
        return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise ApiRateLimitError(_retry_after(exc.headers)) from None
        raise DdakToolError(ErrorCode.AI_UNAVAILABLE, f"API 오류 {exc.code}") from None
    except TimeoutError:
        raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "API 타임아웃") from None
    except urllib.error.URLError as exc:
        timed_out = isinstance(exc.reason, TimeoutError)
        raise DdakToolError(
            ErrorCode.AI_UNAVAILABLE, "API 타임아웃" if timed_out else "API 연결 실패"
        ) from None
    except (ValueError, KeyError, IndexError, TypeError):
        raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "API 응답 형식 오류") from None


class AnthropicApiProvider:
    name = "api"

    def __init__(
        self,
        api_key: str | None,
        *,
        max_tokens: int = MAX_TOKENS,
        client: Any | None = None,
    ) -> None:
        self._key = api_key
        self._max_tokens = max_tokens
        self._client = client

    def __repr__(self) -> str:
        return f"{type(self).__name__}(key=***)"

    def complete(self, req: AIRequest) -> AIResponse:
        if not self._key:
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "DDAK_LLM_API_KEY가 없다")
        if not req.model:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "DDAK_LLM_MODEL이 비어 있다")
        client = self._client or anthropic.Anthropic(
            api_key=self._key,
            timeout=req.timeout_s,
            max_retries=0,
        )
        output_config: OutputConfigParam | anthropic.Omit = anthropic.omit
        if req.json_schema:
            output_config = cast(
                OutputConfigParam,
                {"format": {"type": "json_schema", "schema": _claude_json_schema(req.json_schema)}},
            )
        started = time.monotonic()
        try:
            response = client.messages.create(
                model=req.model,
                max_tokens=self._max_tokens,
                system=req.system,
                messages=[{"role": "user", "content": req.user}],
                output_config=output_config,
            )
        except anthropic.RateLimitError as exc:
            raise ApiRateLimitError(_retry_after(exc.response.headers)) from None
        except anthropic.APITimeoutError:
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "API 타임아웃") from None
        except anthropic.APIConnectionError:
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "API 연결 실패") from None
        except anthropic.APIStatusError as exc:
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, f"API 오류 {exc.status_code}") from None
        try:
            text = next(block.text for block in response.content if block.type == "text")
            tin = int(response.usage.input_tokens)
            tout = int(response.usage.output_tokens)
        except (AttributeError, StopIteration, TypeError, ValueError):
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "API 응답 형식 오류") from None
        if response.stop_reason in {"refusal", "max_tokens"}:
            reason = "API 요청 거절" if response.stop_reason == "refusal" else "API 출력 길이 초과"
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, reason)
        input_price, output_price = _CLAUDE_PRICES_PER_MILLION.get(req.model, (0.0, 0.0))
        usage = AIUsage(
            model=req.model,
            input_tokens=tin,
            output_tokens=tout,
            cost_usd=(tin * input_price + tout * output_price) / 1_000_000,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return AIResponse(text=text, source=Source.LIVE, usage=usage)
