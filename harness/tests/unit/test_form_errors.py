"""관리 폼의 fetch 오류·무스크립트 복귀·세션별 일회성 오류를 실제 라우터로 검증."""

from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.web import form_errors
from tests.unit import test_setup_actions_fix11 as actions_support
from tests.unit.test_setup_actions_fix11 import ARGS
from tests.unit.test_setup_actions_fix11 import coordinator as coordinator
from tests.unit.test_setup_web_fix11 import panel as panel
from tests.unit.test_ui_integration_fix10 import PROJECT, client_for, prepare
from tests.unit.test_ui_integration_fix10 import rig as rig

actions_panel = actions_support.panel


class Boxes(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.last_form = None
        self.visible = []
        self.details = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "details":
            self.details.append("open" in attrs)
        if tag == "input" and attrs.get("name") == "_form_id":
            self.last_form = attrs["value"]
        if (
            tag == "section"
            and "data-form-error" in attrs
            and "hidden" not in attrs
            and all(self.details)
        ):
            self.visible.append(self.last_form)

    def handle_endtag(self, tag):
        if tag == "details":
            self.details.pop()


def submit(client, path, origin, ident, *, js=False, **fields):
    return client.post(
        path,
        data={
            "csrf_token": client.cookies.get("ddak_csrf"),
            "project": PROJECT,
            "_return_to": origin,
            "_form_id": ident,
            **fields,
        },
        headers={
            "Origin": "http://127.0.0.1:8765",
            "Accept": "text/html",
            **({"X-Ddak-Form": "1"} if js else {}),
        },
        follow_redirects=False,
    )


@pytest.mark.parametrize("js", [False, True])
@pytest.mark.parametrize(
    "path,origin,ident,method,fields",
    [
        (
            "/settings",
            "/settings",
            "settings",
            "save_project_settings",
            {"default_targets": "onprem"},
        ),
        ("/settings/deploy", "/", "deploy", "enqueue_deployment", {}),
        ("/ops/plan", "/ops", "plan", "enqueue_deployment", {}),
        ("/ops/unlock", "/ops", "unlock", "unlock_project", {}),
        (
            "/runs/run-form/approval",
            "/runs/run-form/approval",
            "approval",
            "approve",
            {"decision": "approved"},
        ),
        (
            "/runs/run-form/approval",
            "/runs/run-form/approval",
            "approval",
            "approve",
            {"decision": "denied"},
        ),
    ],
)
def test_representative_forms_error_contract(
    rig, monkeypatch, path, origin, ident, method, fields, js
):
    service, source, _ = rig
    monkeypatch.setenv("DDAK_WATCH_PROJECT", PROJECT)
    service.save_project_settings(
        PROJECT,
        {"repo_url": "https://github.com/fixture/app.git"},
        updated_by="operator",
        expected_version=0,
    )
    if path.startswith("/runs/"):
        prepare(service, source, "run-form")
    seen = []

    def fail(*args, **kwargs):
        seen.append((args, kwargs))
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "선행 조건 <확인> 필요")

    monkeypatch.setattr(service, method, fail)
    origin += "?project=" + PROJECT
    with client_for(service) as client:
        assert client.get(origin).status_code == 200
        response = submit(client, path, origin, ident, js=js, **fields)
        assert len(seen) == 1
        if js:
            assert response.status_code == (400 if path == "/settings" else 409)
            assert response.json() == {
                "error": {"code": "PRECONDITION_FAILED", "message": "선행 조건 <확인> 필요"}
            }
            assert "location" not in response.headers
        else:
            assert response.status_code == 303
            assert response.headers["location"].startswith(origin + "&")
            response = client.get(response.headers["location"], headers={"Accept": "text/html"})
            assert response.status_code == 200 and "PRECONDITION_FAILED" in response.text
            assert "선행 조건 &lt;확인&gt; 필요" in response.text
            assert Boxes(response.text).visible == [ident]
        assert response.headers["cache-control"] == "no-store"


def test_setup_flash_is_session_page_project_bound_and_not_overwritten(panel):
    client, coordinator = panel
    secret = "fixture-" + "opaque-private-input"
    coordinator.errors["save_key"] = DdakToolError(
        ErrorCode.CONFIG_INVALID, "키 형식 오류 " + secret
    )
    urls = []
    for provider in ("custom", "judge"):
        response = submit(
            client,
            "/setup/key",
            "/setup?project=other",
            "setup-key-" + provider,
            project="other",
            provider_id=provider,
            value=secret,
        )
        assert response.status_code == 303
        assert secret not in str(response.headers)
        urls.append(response.headers["location"])
    cache = client.app.state.form_flashes
    assert len(cache) == 2 and secret not in repr(cache)
    flash = parse_qs(urlsplit(urls[0]).query)["_form_error"][0]
    wrong_page = client.get("/setup?" + urlencode({"project": "elsewhere", "_form_error": flash}))
    assert Boxes(wrong_page.text).visible == [] and len(cache) == 2
    saved_cookie = client.cookies.get("ddak_csrf")
    client.cookies.clear()
    client.get(urls[0])
    assert len(cache) == 2  # 다른 세션은 읽거나 소비하지 못한다.
    client.cookies.clear()
    client.cookies.set("ddak_csrf", saved_cookie)
    for url, provider in zip(urls, ("custom", "judge"), strict=True):
        response = client.get(url, headers={"Accept": "text/html"})
        assert Boxes(response.text).visible == ["setup-key-" + provider]
        assert "키 형식 오류" in response.text and secret not in response.text
        assert not Boxes(client.get(url).text).visible
    assert not cache


@pytest.mark.parametrize(
    "destination", ["//evil.test", "https://evil.test", "http://[", "/setup?token=private"]
)
def test_redirect_target_does_not_leak_or_leave_product(panel, destination):
    client, coordinator = panel
    coordinator.errors["save_choices"] = RuntimeError("private-sdk-output")
    response = submit(client, "/setup/choices", destination, "setup-choices", project="other")
    assert response.status_code == 303
    url = response.headers["location"]
    assert url.startswith("/setup?") and "evil" not in url and "private" not in url
    response = client.get(url, headers={"Accept": "text/html"})
    assert "INTERNAL" in response.text and "private-sdk-output" not in response.text


def test_csrf_failure_never_mutates_and_still_uses_inline_error_contract(panel):
    client, coordinator = panel
    for js in (False, True):
        response = submit(
            client,
            "/setup/choices",
            "/setup?project=other",
            "setup-choices",
            js=js,
            project="other",
            csrf_token="invalid",
        )
        assert not coordinator.calls
        if js:
            assert response.status_code == 403
            assert response.json()["error"] == {"code": "HTTP_403", "message": "CSRF 검증 실패"}
        else:
            assert response.status_code == 303
            response = client.get(response.headers["location"])
            assert "CSRF 검증 실패" in response.text
            coordinator.calls.clear()


def test_flash_expiry_and_capacity(panel, monkeypatch):
    client, coordinator = panel
    coordinator.errors["save_choices"] = ValueError("private-validation-input")
    monkeypatch.setattr(form_errors, "FLASH_LIMIT", 2)
    for _ in range(3):
        response = submit(
            client, "/setup/choices", "/setup?project=other", "setup-choices", project="other"
        )
        assert response.status_code == 303
    cache = client.app.state.form_flashes
    assert len(cache) == 2
    for record in cache.values():
        record["expires"] = 0
    response = client.get(response.headers["location"])
    assert not cache and not Boxes(response.text).visible


@pytest.mark.parametrize("payload", [b"x" * (65 * 1024), b"\xff"])
def test_inventory_parse_failure_preserves_nondefault_project_and_form(panel, payload):
    client, service = panel
    body = (
        urlencode({"_form_id": "setup-inventory", "_return_to": "/setup?project=other"}).encode()
        + b"&inventory="
        + payload
    )
    response = client.post(
        "/setup/inventory",
        content=body,
        headers={"Accept": "text/html", "Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/setup?project=other&")
    assert not service.calls
    page = client.get(response.headers["location"])
    assert Boxes(page.text).visible == ["setup-inventory"]
    assert ("HTTP_413" if len(payload) > 65536 else "HTTP_400") in page.text


@pytest.mark.parametrize("uncertain", [False, True])
def test_failed_action_keeps_error_inside_its_summary(actions_panel, uncertain):
    client, actions, adapter, store = actions_panel
    response = submit(
        client,
        "/setup/actions/plan",
        "/setup/actions?project=demo",
        "action-database",
        project="demo",
        kind="database",
        **ARGS,
    )
    assert response.status_code == 200  # 위치 메타는 엄격한 업무 입력 검사에서 제외된다.
    public = actions.view("demo")["actions"][-1]
    error = DdakToolError(ErrorCode.PRECONDITION_FAILED, "준비 작업 실패")
    error.needs_human = uncertain
    adapter.failure = error
    response = submit(
        client,
        "/setup/actions/apply",
        "/setup/actions?project=demo",
        "action-apply-" + public["id"],
        project="demo",
        id=public["id"],
        approved_hash=public["hash"],
    )
    assert response.status_code == 303
    assert store.run(public["id"])["status"] == ("NEEDS_HUMAN" if uncertain else "CANCELLED")
    page = client.get(response.headers["location"])
    article = page.text.split(f'<article data-action="{public["id"]}">', 1)[1].split(
        "</article>", 1
    )[0]
    assert "PRECONDITION_FAILED" in article and "준비 작업 실패" in article
    assert 'role="alert"' in article and "<form" not in article


@pytest.mark.parametrize(
    "ident,method,fields",
    [
        ("plan", "enqueue_deployment", {"ref": "v2"}),
        ("unlock", "unlock_project", {"reason": "상태 확인"}),
    ],
)
def test_dashboard_operating_error_expands_its_details(rig, monkeypatch, ident, method, fields):
    service, _, _ = rig

    def fail(*args, **kwargs):
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "운영 요청 실패")

    monkeypatch.setattr(service, method, fail)
    origin = "/?project=" + PROJECT
    with client_for(service) as client:
        client.get(origin)
        response = submit(client, "/ops/" + ident, origin, ident, **fields)
        assert response.status_code == 303
        page = client.get(response.headers["location"])
        assert Boxes(page.text).visible == [ident]
        assert "<details open><summary>운영 도구</summary>" in page.text
