"""Fake 어댑터: 실제 HTTP 없이 v1 flaskr처럼 응답한다(테스트·드라이런·UI 개발). source=fixture."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.verify.smoke.logic import HttpClient, Response


class FakeFlaskr:
    """v1 익명 게시판의 응답 모양만 흉내 낸다. 상태는 인스턴스 안에만 있다."""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._titles: list[str] = []

    def request(
        self, method: str, path: str, form: Mapping[str, str] | None, timeout: float
    ) -> Response:
        del timeout
        if method == "GET" and path == "/version":
            body = {
                "release_id": self._run_id,
                "schema_expected": "0001",
                "app_env": "fake",
                "base_url": "http://fake.invalid",
                "db": {"dialect": "mysql", "host": None, "tls": False, "tls_verified": False},
            }
            return Response(200, (("Content-Type", "application/json"),), json.dumps(body))
        if method == "GET" and path == "/health/ready":
            body = {"status": "ok", "db": "ok", "schema": {"current": "0001", "expected": "0001"}}
            return Response(200, (("Content-Type", "application/json"),), json.dumps(body))
        if method == "GET" and path == "/":
            items = "".join(
                f'<article class="post"><h1>{t}</h1></article>' for t in reversed(self._titles)
            )
            return Response(200, (), f"<nav>Flaskr</nav><h1>Posts</h1>{items}")
        if method == "POST" and path == "/create":
            title = (form or {}).get("title", "").strip()
            if not title:
                return Response(200, (), '<div class="error">Title is required.</div>')
            if len(title) > 200:
                return Response(200, (), '<div class="error">Title is too long.</div>')
            self._titles.append(title)
            return Response(302, (("Location", "/"),), "")
        return Response(404, (), "")


class FakeSmokeAdapter:
    name = "fake"
    source: Literal["live", "fixture"] = "fixture"

    def __init__(self, target: Target) -> None:
        self._target = target

    @property
    def target(self) -> Target:
        return self._target

    def client(self, ctx: RunContext) -> HttpClient:
        return FakeFlaskr(ctx.run_id)
