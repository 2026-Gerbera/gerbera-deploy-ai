"""Groq API 생성 어댑터. CLI 지원 없음. 재시도/검증은 공통 gateway가 맡는다."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from ddak.core.ai.providers.types import AIRequest, AIResponse
from ddak.core.contracts.base import AIUsage
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode

# 출력은 작은 JSON이다. 잘리면 스키마 파싱 실패로 드러난다.
MAX_TOKENS = 8192
# TEMP(groq)
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


def groq_chat(
    *, url: str, key: str, model: str, system: str, user: str, timeout_s: float, max_tokens: int
) -> tuple[str, int, int]:
    """chat 1회 -> (text, input_tokens, output_tokens). 실패는 AI_UNAVAILABLE. jev.py도 쓴다."""
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
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
        reason = "API 속도 제한(429)" if exc.code == 429 else f"API 오류 {exc.code}"
        raise DdakToolError(ErrorCode.AI_UNAVAILABLE, reason) from None
    except TimeoutError:
        raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "API 타임아웃") from None
    except urllib.error.URLError as exc:
        timed_out = isinstance(exc.reason, TimeoutError)
        raise DdakToolError(
            ErrorCode.AI_UNAVAILABLE, "API 타임아웃" if timed_out else "API 연결 실패"
        ) from None
    except (ValueError, KeyError, IndexError, TypeError):
        raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "API 응답 형식 오류") from None


class GroqApiProvider:
    name = "groq"

    def __init__(self, api_key: str | None, *, max_tokens: int = MAX_TOKENS) -> None:
        self._key = api_key
        self._max_tokens = max_tokens

    def __repr__(self) -> str:
        return f"{type(self).__name__}(key=***)"

    def complete(self, req: AIRequest) -> AIResponse:
        if not self._key:
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "Groq API 키가 없다")
        if not req.model:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "DDAK_LLM_MODEL이 비어 있다")
        system = req.system
        if req.json_schema:
            system += "\n\nRespond with a single JSON object matching this JSON Schema:\n" + (
                json.dumps(dict(req.json_schema))
            )
        else:
            system += "\n\nRespond with a single JSON object."
        started = time.monotonic()
        text, tin, tout = groq_chat(
            url=GROQ_ENDPOINT,
            key=self._key,
            model=req.model,
            system=system,
            user=req.user,
            timeout_s=req.timeout_s,
            max_tokens=self._max_tokens,
        )
        usage = AIUsage(
            model=req.model,
            input_tokens=tin,
            output_tokens=tout,
            cost_usd=0.0,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return AIResponse(text=text, source=Source.LIVE, usage=usage)
