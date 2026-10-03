"""관리 웹 골격 테스트. HTTP 클라이언트 의존성 없이 라우트와 핸들러만 확인한다."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI, Request, Response

from ddak.app import create
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.web.app import create_app
from ddak.web.routes import approvals, settings

pytestmark = pytest.mark.anyio


def _endpoint(app: FastAPI, path: str) -> Callable[..., Any]:
    return next(r.endpoint for r in app.routes if getattr(r, "path", None) == path)  # type: ignore[attr-defined]


async def test_healthz_and_routes() -> None:
    app = create_app()
    assert await _endpoint(app, "/healthz")() == {"ok": True}
    paths = {getattr(route, "path", "") for route in app.routes}
    for route in app.routes:
        included = getattr(route, "original_router", None)
        paths.update(getattr(child, "path", "") for child in getattr(included, "routes", ()))
    assert {
        "/healthz",
        "/api/llm-status",
        "/events",
        "/api/chat",
        "/settings/deploy",
    } <= paths


async def test_llm_status_is_injected_by_app(monkeypatch: pytest.MonkeyPatch) -> None:
    no_status = await _endpoint(create_app(), "/api/llm-status")()
    assert no_status["ok"] is False
    monkeypatch.setenv("DDAK_LLM_BACKEND", "replay")  # 테스트에서 CLI를 실행하지 않게
    injected = await _endpoint(create(), "/api/llm-status")()
    assert injected["backend"] == "replay"


async def test_approval_page_shows_exact_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    patch = "+SECRET_KEY=proposed-value\n" + ("+x = 1\n" * 700)
    captured: dict[str, Any] = {}

    class Service:
        @staticmethod
        def get_run(run_id: str) -> dict[str, str]:
            return {"status": "AWAITING_APPROVAL"}

        @staticmethod
        def approval_view(run_id: str) -> dict[str, Any]:
            return {"run_id": run_id, "patch": patch}

    class Templates:
        @staticmethod
        def TemplateResponse(*, request: Request, name: str, context: dict[str, Any]) -> Response:
            del request, name
            captured.update(context)
            return Response()

    request = Request(
        {"type": "http", "method": "GET", "path": "/runs/run-1/approval", "headers": []}
    )
    monkeypatch.setattr(approvals, "deployment", lambda request: Service())
    monkeypatch.setattr(approvals, "templates", Templates())
    monkeypatch.setattr(approvals, "issue_csrf", lambda *args: None)

    await approvals.approval_page(request, "run-1")

    assert captured["approval"]["patch"] is None


async def test_failed_preparation_redirects_to_result(monkeypatch: pytest.MonkeyPatch) -> None:
    class Service:
        @staticmethod
        def get_run(run_id: str) -> dict[str, str]:
            return {"status": "AWAITING_APPROVAL"}

        @staticmethod
        def approval_view(run_id: str) -> dict[str, Any]:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "준비 실패")

    request = Request(
        {"type": "http", "method": "GET", "path": "/runs/run-1/approval", "headers": []}
    )
    monkeypatch.setattr(approvals, "deployment", lambda request: Service())

    response = await approvals.approval_page(request, "run-1")

    assert response.status_code == 303
    assert response.headers["location"] == "/runs/run-1/result"


def _form_request(path: str, body: str) -> Request:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body.encode(), "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        },
        receive,
    )


async def test_settings_save_real_deploy_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Service:
        @staticmethod
        def save_project_settings(project: str, data: dict[str, Any], **kwargs: Any) -> None:
            captured.update({"project": project, "data": data, **kwargs})

    body = (
        "csrf_token=x&project=flaskr&version=3&"
        "repo_url=https%3A%2F%2Fgithub.com%2Forg%2Fapp.git&watch_branch=prod&"
        "default_targets=cloud&auto_detect=on&cloud_domain=app.example.com&"
        "dns_mode=external&hosted_zone_id="
    )
    monkeypatch.setattr(settings, "deployment", lambda request: Service())
    monkeypatch.setattr(settings, "selected_project", lambda request, project: project)
    monkeypatch.setattr(settings, "require_safe_post", lambda *args: None)

    response = await settings.save_settings(_form_request("/settings", body))

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?project=flaskr&saved=1"
    assert captured["project"] == "flaskr"
    assert captured["expected_version"] == 3
    assert captured["data"] == {
        "repo_url": "https://github.com/org/app.git",
        "watch_branch": "prod",
        "auto_detect": True,
        "code_patch": False,
        "default_targets": "cloud",
        "cloud_domain": "app.example.com",
        "dns_mode": "external",
        "hosted_zone_id": None,
    }


async def test_manual_deploy_enqueues_and_redirects_to_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Service:
        @staticmethod
        def enqueue_deployment(project: str) -> dict:
            assert project == "flaskr"
            return {"request_id": "fixture", "status": "PREPARING"}

    monkeypatch.setattr(settings, "deployment", lambda request: Service())
    monkeypatch.setattr(settings, "selected_project", lambda request, project: project)
    monkeypatch.setattr(settings, "require_safe_post", lambda *args: None)
    response = await settings.request_deploy(
        _form_request("/settings/deploy", "csrf_token=x&project=flaskr")
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?project=flaskr"
