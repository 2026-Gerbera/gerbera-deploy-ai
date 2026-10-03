"""표준 라이브러리로 Claude Messages HTTP 1회. 재시도는 공통 gateway가 맡는다.

계약: https://platform.claude.com/docs/en/api/messages/create
요청·응답 원문과 키는 예외에 넣지 않는다.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Literal

from ddak.core.ai.providers.types import AIRequest, AIResponse
from ddak.core.contracts.base import AIUsage
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode

ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"


class ClaudeApiProvider:
    name = "claude-api"

    def __init__(self, api_key: str | None, *, effort: Literal["low", "medium"] = "low") -> None:
        self._key = api_key
        self._effort = effort

    def __repr__(self) -> str:
        return f"{type(self).__name__}(key=***)"

    def complete(self, req: AIRequest) -> AIResponse:
        if not self._key:
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "anthropic_api_key가 없다")
        model = req.model or DEFAULT_MODEL
        payload = {
            "model": model,
            "max_tokens": 8192,
            "system": req.system
            + "\nReturn only JSON matching this JSON Schema:\n"
            + json.dumps(dict(req.json_schema)),
            "messages": [{"role": "user", "content": req.user}],
            "output_config": {"effort": self._effort},
        }
        started = time.monotonic()
        try:
            http_req = urllib.request.Request(
                ANTHROPIC_ENDPOINT,
                data=json.dumps(payload).encode(),
                headers={
                    "x-api-key": self._key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                    "User-Agent": "ddak/0.1",
                },
                method="POST",
            )
            with urllib.request.urlopen(http_req, timeout=req.timeout_s) as response:  # noqa: S310
                data = json.loads(response.read())
            content = data["content"]
            if (
                not isinstance(content, list)
                or not content
                or any(
                    not isinstance(block, dict)
                    or block.get("type") != "text"
                    or not isinstance(block.get("text"), str)
                    for block in content
                )
            ):
                raise ValueError("invalid content")
            if data.get("stop_reason") in ("max_tokens", "tool_use", "refusal"):
                raise ValueError("incomplete response")
            text = "".join(block["text"] for block in content)
            usage = data.get("usage") or {}
            result = AIUsage(
                model=model,
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                cost_usd=0,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except urllib.error.HTTPError as exc:
            raise DdakToolError(
                ErrorCode.AI_UNAVAILABLE, f"Claude API HTTP 오류 {exc.code}"
            ) from None
        except TimeoutError:
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "Claude API 타임아웃") from None
        except urllib.error.URLError as exc:
            detail = (
                "Claude API 타임아웃"
                if isinstance(exc.reason, TimeoutError)
                else "Claude API 연결 실패"
            )
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, detail) from None
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "Claude API 응답/연결 오류") from None
        return AIResponse(text=text, source=Source.LIVE, usage=result)
