"""api.py / jev.py의 임시 Groq 연결(TEMP(groq)). 로컬 가짜 HTTP 서버만 쓴다. 실호출은 llm 마커."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from pydantic import BaseModel

from ddak.core.ai import gateway
from ddak.core.ai.gateway import ask_jev, call_ai
from ddak.core.ai.providers import AIRequest, api, jev
from ddak.core.ai.providers.api import AnthropicApiProvider
from ddak.core.ai.providers.jev import JevClient, JevQuestion
from ddak.core.config import Settings
from ddak.core.contracts.enums import LLMBackend, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.runtime import tool_context

KEY = "gsk_" + "fake" + "-key-" + "1234"
REQ = AIRequest(
    purpose="generate_plan", system="sys", user="u", json_schema={"type": "object"}, model="m1"
)


class _Out(BaseModel):
    tiers: list[str]


class Fake:
    """status/body/delay를 정해 두는 가짜 서버. seen에 요청을 쌓는다."""

    def __init__(self) -> None:
        self.status = 200
        self.body: Any = b""
        self.delay = 0.0
        self.seen: list[dict[str, Any]] = []
        self.headers: list[Any] = []


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[Fake]:
    state = Fake()

    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            n = int(self.headers["Content-Length"])
            state.seen.append(json.loads(self.rfile.read(n)))
            state.headers.append(self.headers)
            time.sleep(state.delay)
            body = state.body if isinstance(state.body, bytes) else json.dumps(state.body).encode()
            try:
                self.send_response(state.status)
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *a: object) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/chat"
    monkeypatch.setattr(api, "GROQ_ENDPOINT", url)
    monkeypatch.setattr(jev, "JEV_ENDPOINT", url)
    yield state
    srv.shutdown()


def _ok(content: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


def test_api_success(fake: Fake) -> None:
    fake.body = _ok('{"tiers": ["web"]}')
    resp = AnthropicApiProvider(KEY).complete(REQ)
    assert json.loads(resp.text) == {"tiers": ["web"]}
    assert resp.source is Source.LIVE
    assert resp.usage is not None
    assert (resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.model) == (11, 7, "m1")
    sent = fake.seen[0]
    assert sent["model"] == "m1" and sent["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in sent["messages"][0]["content"]
    assert fake.headers[0]["Authorization"] == f"Bearer {KEY}"
    assert fake.headers[0]["User-Agent"].startswith("ddak")


@pytest.mark.parametrize("status", [429, 500, 503])
def test_api_http_errors_are_unavailable(fake: Fake, status: int) -> None:
    fake.status, fake.body = status, b"{}"
    with pytest.raises(DdakToolError) as info:
        AnthropicApiProvider(KEY).complete(REQ)
    assert info.value.code is ErrorCode.AI_UNAVAILABLE
    assert KEY not in str(info.value)


def test_api_timeout(fake: Fake) -> None:
    fake.body, fake.delay = _ok("{}"), 1.0
    req = AIRequest(purpose="p", system="s", user="u", model="m1", timeout_s=0.2)
    with pytest.raises(DdakToolError) as info:
        AnthropicApiProvider(KEY).complete(req)
    assert info.value.code is ErrorCode.AI_UNAVAILABLE and "타임아웃" in str(info.value)


def test_api_connection_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "GROQ_ENDPOINT", "http://127.0.0.1:1/chat")
    with pytest.raises(DdakToolError) as info:
        AnthropicApiProvider(KEY).complete(REQ)
    assert info.value.code is ErrorCode.AI_UNAVAILABLE


def test_api_malformed_response(fake: Fake) -> None:
    fake.body = b"not json"
    with pytest.raises(DdakToolError) as info:
        AnthropicApiProvider(KEY).complete(REQ)
    assert info.value.code is ErrorCode.AI_UNAVAILABLE


def test_api_no_key_and_no_model() -> None:
    with pytest.raises(DdakToolError) as info:
        AnthropicApiProvider(None).complete(REQ)
    assert info.value.code is ErrorCode.AI_UNAVAILABLE
    with pytest.raises(DdakToolError) as info:
        AnthropicApiProvider(KEY).complete(AIRequest(purpose="p", system="s", user="u"))
    assert info.value.code is ErrorCode.CONFIG_INVALID


def test_key_never_in_repr() -> None:
    assert KEY not in repr(AnthropicApiProvider(KEY))
    assert KEY not in repr(JevClient(KEY, model="m", timeout_s=1))


QS = [
    JevQuestion(id="q_noul", kind="noul", text="needs db?"),
    JevQuestion(id="q_choice", kind="choice", text="which?", choices=("a", "b")),
    JevQuestion(id="q_score", kind="score", text="how sure?"),
]


def _jev(timeout: float = 2.0) -> JevClient:
    return JevClient(KEY, model="m2", timeout_s=timeout)


def _answers(**over: Any) -> dict[str, Any]:
    items = [
        {"id": "q_noul", "probability": 0.9},
        {"id": "q_choice", "choice": "b"},
        {"id": "q_score", "confidence": 0.5},
    ]
    return _ok(
        json.dumps({"answers": [i for i in items if i["id"] not in over] + list(over.values())})
    )


def test_jev_three_kinds(fake: Fake) -> None:
    fake.body = _answers()
    out = _jev().ask(state="K=1", questions=QS)
    assert [(a.id, a.probability, a.choice, a.confidence) for a in out] == [
        ("q_noul", 0.9, None, None),
        ("q_choice", None, "b", None),
        ("q_score", None, None, 0.5),
    ]
    assert fake.seen[0]["model"] == "m2"
    assert "<untrusted_data>\nK=1\n</untrusted_data>" in fake.seen[0]["messages"][1]["content"]


@pytest.mark.parametrize(
    "bad",
    [
        {"q_noul": {"id": "q_choice", "choice": "zzz"}},  # 보기에 없는 값 + 누락 id
        {"q_score": {"id": "q_score", "confidence": "high"}},  # 형식 불일치
        {"q_score": {"id": "q_score", "confidence": 7}},  # 범위 밖
    ],
)
def test_jev_invalid_answers(fake: Fake, bad: dict[str, Any]) -> None:
    fake.body = _answers(**bad)
    with pytest.raises(DdakToolError) as info:
        _jev().ask(state="s", questions=QS)
    assert info.value.code is ErrorCode.AI_OUTPUT_INVALID


def test_jev_missing_id_and_non_json(fake: Fake) -> None:
    fake.body = _ok(json.dumps({"answers": [{"id": "q_noul", "probability": 0.1}]}))
    with pytest.raises(DdakToolError) as info:
        _jev().ask(state="s", questions=QS)
    assert info.value.code is ErrorCode.AI_OUTPUT_INVALID
    fake.body = _ok("hello")
    with pytest.raises(DdakToolError) as info:
        _jev().ask(state="s", questions=QS)
    assert info.value.code is ErrorCode.AI_OUTPUT_INVALID


def test_jev_network_and_key(fake: Fake) -> None:
    fake.status, fake.body = 500, b"{}"
    with pytest.raises(DdakToolError) as info:
        _jev().ask(state="s", questions=QS)
    assert info.value.code is ErrorCode.AI_UNAVAILABLE and KEY not in str(info.value)
    with pytest.raises(DdakToolError) as info:
        JevClient(None, model="m", timeout_s=1).ask(state="s", questions=QS)
    assert info.value.code is ErrorCode.AI_UNAVAILABLE


def test_gateway_end_to_end(fake: Fake) -> None:
    cfg = Settings(
        llm_backend=LLMBackend.API,
        llm_api_key=KEY,
        llm_model="m1",
        jev_api_key=KEY,
        jev_model="m2",
        ai_retries=0,
    )
    with tool_context("generate_plan", "run-1"):
        fake.body = _ok('{"tiers": ["web"]}')
        res = call_ai(instruction="x", data="d", output_model=_Out, settings=cfg)
        assert res.value.tiers == ["web"] and res.source is Source.LIVE and res.usage is not None
        fake.body = _answers()
        assert len(ask_jev(state="s", questions=QS, settings=cfg)) == 3
    assert gateway.JevClient is JevClient


@pytest.mark.llm
def test_live_groq_smoke() -> None:
    key, model = os.environ.get("DDAK_LLM_API_KEY"), os.environ.get("DDAK_LLM_MODEL")
    if not key or not model:
        pytest.skip("DDAK_LLM_API_KEY / DDAK_LLM_MODEL 없음")
    req = AIRequest(purpose="generate_plan", system="Return JSON.", user='{"ok":true}', model=model)
    assert json.loads(AnthropicApiProvider(key).complete(req).text)
