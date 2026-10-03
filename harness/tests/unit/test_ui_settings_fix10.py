"""수정10: 설정 기본값·프로젝트 선택·도메인·비동기 배포 연결을 외부 실행 없이 검증한다."""

from __future__ import annotations

import asyncio
import json
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI, HTTPException, Request

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.defaults import project_values
from ddak.core.project_settings import ProjectSettings
from ddak.executor.service import DeploymentService
from ddak.web.routes import ops, settings

pytestmark = pytest.mark.anyio
TOKEN = "local-csrf-test-" + "x" * 32


class SettingsStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.writes: list[dict[str, Any]] = []

    def project_settings(self, project: str) -> dict[str, Any] | None:
        return self.rows.get(project)

    def list_projects(self) -> list[str]:
        return sorted(self.rows)

    def list_project_settings(self) -> list[dict[str, Any]]:
        return [{**row, "project": name} for name, row in sorted(self.rows.items())]

    def save_project_settings(self, project: str, data: dict[str, Any], **kwargs: Any) -> dict:
        version = (self.rows.get(project) or {}).get("version", 0)
        if kwargs["expected_version"] != version:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "설정 버전 충돌")
        # 저장 검증은 병합과 같은 트랜잭션 안에서 Store가 수행한다.
        data = ProjectSettings.model_validate(data).model_dump(mode="json", exclude_unset=True)
        self.writes.append({"project": project, "data": data, **kwargs})
        self.rows[project] = {**data, "version": version + 1}
        return self.rows[project]


class Service(DeploymentService):
    """파일·실제 실행 없이 공개 서비스의 선택·저장·enqueue 로직을 사용한다."""

    def __init__(self) -> None:
        self.store = SettingsStore()
        self._preparation_tasks = {}
        self._preparation_requests = {}
        self._preparing_runs = {}
        self.planning_flow = None

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return []

    def get_run(self, run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "status": "AWAITING_APPROVAL"}


class FormMarkup(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.fields: dict[str, dict[str, str | None]] = {}
        self.selected: list[str | None] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"input", "select"} and values.get("name"):
            self.fields[values["name"]] = values
        if tag == "option" and "selected" in values:
            self.selected.append(values.get("value"))


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Service:
    monkeypatch.setenv("DDAK_WATCH_PROJECT", "flaskr-three")
    return Service()


def request(service: Service, path: str = "/settings", form: dict | None = None) -> Request:
    app = FastAPI()
    app.state.deployment = service
    app.state.settings = SimpleNamespace(admin_port=8765, aws_profile=None, setting_sources={})
    headers = [(b"host", b"127.0.0.1:8765")]
    if form is not None:
        headers.extend(
            [
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"origin", b"http://127.0.0.1:8765"),
                (b"cookie", f"ddak_csrf={TOKEN}".encode()),
            ]
        )
    body = urlencode({"csrf_token": TOKEN, **(form or {})}).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "app": app,
            "method": "GET" if form is None else "POST",
            "path": path,
            "scheme": "http",
            "query_string": b"",
            "headers": headers,
            "server": ("127.0.0.1", 8765),
        },
        receive,
    )


async def test_unsaved_settings_use_product_defaults_and_watch_project(service: Service) -> None:
    response = await settings.settings_page(request(service))
    html = response.body.decode()
    markup = FormMarkup(html)
    actual = response.context["settings"]
    assert {key: actual[key] for key in ProjectSettings.model_fields} == project_values({})
    assert actual["setting_sources"]["watch_branch"] == "기본 파일"
    assert markup.fields["project"]["value"] == "flaskr-three"
    assert markup.fields["watch_branch"]["value"] == "prod"
    assert markup.selected == ["onprem", "external"]
    assert "required" not in markup.fields["cloud_domain"]
    assert "<title>프로젝트 설정 · Gerbera</title>" in html
    assert "ddak_csrf=" in response.headers["set-cookie"]
    assert service.store.writes == []


@pytest.mark.parametrize("target", ["onprem", "cloud", "both"])
async def test_saved_settings_and_explicit_project_render(service: Service, target: str) -> None:
    service.store.rows["other"] = {
        "default_targets": target,
        "cloud_domain": "app.example.com",
        "version": 7,
    }
    response = await settings.settings_page(request(service), "other")
    markup = FormMarkup(response.body.decode())
    assert markup.fields["project"]["value"] == "other"
    assert markup.fields["version"]["value"] == "7"
    assert markup.fields["watch_branch"]["value"] == "prod"
    assert markup.fields["cloud_domain"]["value"] == "app.example.com"
    assert markup.selected == [target, "external"]
    assert ("required" in markup.fields["cloud_domain"]) == (target != "onprem")
    assert "cloud_domain" in markup.fields["default_targets"]["onchange"]
    assert "!== 'onprem'" in markup.fields["default_targets"]["onchange"]


@pytest.mark.parametrize("explicit", [None, "flaskr", "other"])
async def test_get_fallback_and_demo_alias(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    explicit: str | None,
) -> None:
    monkeypatch.delenv("DDAK_WATCH_PROJECT")
    service.store.rows["demo"] = {"watch_branch": "demo-prod"}
    response = await settings.settings_page(request(service), explicit)
    assert response.context["project"] == ("other" if explicit == "other" else "demo")
    service.store.rows["flaskr"] = {"watch_branch": "actual-prod"}
    response = await settings.settings_page(request(service), "flaskr")
    assert response.context["project"] == "flaskr"
    assert response.context["settings"]["watch_branch"] == "actual-prod"


@pytest.mark.parametrize("project,expected", [(None, "flaskr-three"), ("other", "other")])
async def test_save_onprem_without_domain(
    service: Service, project: str | None, expected: str
) -> None:
    form = {"repo_url": "https://github.com/org/app.git", "auto_detect": "on"}
    if project is not None:
        form["project"] = project
    response = await settings.save_settings(request(service, form=form))
    saved = service.store.writes[0]
    assert response.status_code == 303
    assert response.headers["location"] == f"/settings?project={expected}&saved=1"
    assert saved["project"] == expected
    assert saved["updated_by"] == "local-operator"
    assert saved["expected_version"] == 0
    assert saved["data"] == {
        "repo_url": "https://github.com/org/app.git",
        "watch_branch": "prod",
        "auto_detect": True,
        "code_patch": False,
        "default_targets": "onprem",
        "cloud_domain": None,
        "dns_mode": "external",
        "hosted_zone_id": None,
    }


async def test_save_resolves_demo_and_keeps_version(service: Service) -> None:
    service.store.rows["demo"] = {"version": 3}
    response = await settings.save_settings(
        request(
            service,
            form={
                "project": "flaskr",
                "version": "3",
                "default_targets": "onprem",
                "cloud_domain": "  ",
            },
        )
    )
    assert response.headers["location"] == "/settings?project=demo&saved=1"
    assert service.store.writes[0]["expected_version"] == 3
    with pytest.raises(HTTPException) as error:
        await settings.save_settings(request(service, form={"project": "demo", "version": "3"}))
    assert error.value.status_code == 400
    assert len(service.store.writes) == 1


@pytest.mark.parametrize("target", ["onprem", "cloud", "both"])
async def test_supplied_domain_is_normalized(service: Service, target: str) -> None:
    await settings.save_settings(
        request(
            service,
            form={
                "default_targets": target,
                "cloud_domain": " App.Example.Com. ",
                "dns_mode": "route53",
                "hosted_zone_id": " Z123456 ",
            },
        )
    )
    saved = service.store.writes[0]["data"]
    assert saved["cloud_domain"] == "app.example.com"
    assert saved["dns_mode"] == "route53"
    assert saved["hosted_zone_id"] == "Z123456"


@pytest.mark.parametrize("target", ["cloud", "both"])
@pytest.mark.parametrize(
    "domain", ["", "  ", "example.com", "https://app.example.com", "x.amazonaws.com"]
)
async def test_cloud_targets_keep_domain_validation(
    service: Service, target: str, domain: str
) -> None:
    with pytest.raises(HTTPException) as error:
        await settings.save_settings(
            request(
                service,
                form={
                    "default_targets": target,
                    "cloud_domain": domain,
                    "dns_mode": "external",
                },
            )
        )
    assert error.value.status_code == 400
    assert service.store.writes == []


@pytest.mark.parametrize(
    "form",
    [
        {"default_targets": "onprem", "cloud_domain": "invalid"},
        {"default_targets": "cloud", "cloud_domain": "app.example.com", "dns_mode": "route53"},
        {"default_targets": "both", "cloud_domain": "app.example.com", "hosted_zone_id": "bad"},
        {"default_targets": "unknown"},
        {"dns_mode": "unknown"},
        {"version": "bad"},
        {"project": "../other"},
    ],
)
async def test_invalid_settings_are_not_saved(service: Service, form: dict) -> None:
    with pytest.raises(HTTPException) as error:
        await settings.save_settings(request(service, form=form))
    assert error.value.status_code == 400
    assert service.store.writes == []


@pytest.mark.parametrize(
    "handler,path",
    [
        (settings.save_settings, "/settings"),
        (settings.request_deploy, "/settings/deploy"),
    ],
)
async def test_post_keeps_csrf_validation(service: Service, handler: Any, path: str) -> None:
    with pytest.raises(HTTPException) as error:
        await handler(request(service, path, {"csrf_token": "wrong"}))
    assert error.value.status_code == 403
    assert service.store.writes == []
    assert service._preparation_tasks == {}


@pytest.mark.parametrize(
    "handler,path,status",
    [
        (settings.save_settings, "/settings", 400),
        (settings.request_deploy, "/settings/deploy", 409),
    ],
)
@pytest.mark.parametrize("exception", [DdakToolError, ValueError])
async def test_service_errors_are_redacted(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    path: str,
    status: int,
    exception: type[Exception],
) -> None:
    secret = "fake-" + "private-value"

    def fail(*args: Any, **kwargs: Any) -> None:
        message = "password=" + secret
        if exception is DdakToolError:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, message)
        raise ValueError(message)

    method = "save_project_settings" if path == "/settings" else "enqueue_deployment"
    monkeypatch.setattr(service, method, fail)
    with pytest.raises(HTTPException) as error:
        await handler(request(service, path, {}))
    assert error.value.status_code == status
    assert secret not in error.value.detail
    if exception is DdakToolError:
        assert "[REDACTED]" in error.value.detail
    else:
        assert "입력 형식 오류" in error.value.detail


@pytest.mark.parametrize(
    "project,expected", [(None, "flaskr-three"), ("flaskr", "demo"), ("other", "other")]
)
async def test_deploy_redirects_immediately_and_deduplicates_with_ops(
    service: Service,
    project: str | None,
    expected: str,
) -> None:
    service.store.rows[expected] = {"repo_url": "https://github.com/org/app.git"}
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list[str] = []

    async def planning_flow(current: DeploymentService, deployment_request: Any) -> str:
        seen.append(deployment_request.project)
        started.set()
        await release.wait()
        return "run-test"

    service.planning_flow = planning_flow
    form = {"project": project} if project is not None else {}
    try:
        first = await settings.request_deploy(request(service, "/settings/deploy", form))
        second = await settings.request_deploy(request(service, "/settings/deploy", form))
        queued = await ops.operate(request(service, "/ops/plan", form), "plan")
        assert first.status_code == second.status_code == 303
        assert first.headers["location"] == second.headers["location"] == f"/?project={expected}"
        assert queued.status_code == 202
        preparations = service.list_preparations(expected)
        assert len(preparations) == len(service._preparation_tasks) == 1
        assert json.loads(queued.body)["request_id"] == preparations[0]["request_id"]
        assert not started.is_set()
        await asyncio.wait_for(started.wait(), timeout=1)
        assert seen == [expected]
    finally:
        release.set()
        await asyncio.gather(*service._preparation_tasks.values())
    assert service.list_preparations(expected)[0]["status"] == "AWAITING_APPROVAL"
