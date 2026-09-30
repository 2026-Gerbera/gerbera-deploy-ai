"""관리 웹 골격 테스트. HTTP 클라이언트 의존성 없이 라우트와 핸들러만 확인한다."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI

from ddak.app import create
from ddak.web.app import create_app

pytestmark = pytest.mark.anyio


def _endpoint(app: FastAPI, path: str) -> Callable[..., Any]:
    return next(r.endpoint for r in app.routes if getattr(r, "path", None) == path)  # type: ignore[attr-defined]


async def test_healthz_and_routes() -> None:
    app = create_app()
    assert await _endpoint(app, "/healthz")() == {"ok": True}
    paths = {getattr(route, "path", "") for route in app.routes}
    assert {"/healthz", "/api/llm-status", "/events", "/api/chat"} <= paths


async def test_llm_status_is_injected_by_app(monkeypatch: pytest.MonkeyPatch) -> None:
    no_status = await _endpoint(create_app(), "/api/llm-status")()
    assert no_status["ok"] is False
    monkeypatch.setenv("DDAK_LLM_BACKEND", "replay")  # 테스트에서 CLI를 실행하지 않게
    injected = await _endpoint(create(), "/api/llm-status")()
    assert injected["backend"] == "replay"
