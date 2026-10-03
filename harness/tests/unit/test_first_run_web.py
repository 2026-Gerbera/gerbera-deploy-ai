"""source=fixture: 실제 setup/settings 서비스의 폼 왕복과 오류 표시 회귀."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ddak import app
from ddak.core.config import Settings
from ddak.core.defaults import project_values
from ddak.web.routes import settings as settings_routes
from ddak.web.routes import setup as setup_routes
from tests.unit.test_first_run import REPO
from tests.unit.test_first_run import first_run as first_run

PRIVATE = "fixture-private-" + "must-not-appear"


class Forms(HTMLParser):
    """브라우저가 제출하는 이름 있는 입력을 읽는다. 비밀값 재표시는 금지한다."""

    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.select = None
        self.textarea = None
        self.alerts = []
        self.sources = {}
        self.source_field = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("role") == "alert" and "hidden" not in attrs:
            self.alerts.append(self.getpos())
        if "data-setting-source" in attrs:
            self.source_field = attrs["data-setting-source"]
            self.sources[self.source_field] = ""
        if tag == "form":
            self.current = {"action": attrs.get("action"), "fields": {}, "controls": []}
            self.forms.append(self.current)
        if self.current is None:
            return
        name = attrs.get("name")
        if tag in {"input", "select", "textarea"} and name:
            self.current["controls"].append((tag, attrs, self.getpos()))
        if tag == "input" and name and "disabled" not in attrs:
            if attrs.get("type") in {"checkbox", "radio"} and "checked" not in attrs:
                return
            self.current["fields"][name] = attrs.get(
                "value", "on" if attrs.get("type") == "checkbox" else ""
            )
        elif tag == "select" and name:
            self.select = name
        elif tag == "option" and self.select:
            fields = self.current["fields"]
            if self.select not in fields or "selected" in attrs:
                fields[self.select] = attrs.get("value", "")
        elif tag == "textarea" and name:
            self.textarea = name
            self.current["fields"][name] = ""

    def handle_data(self, data):
        if self.source_field:
            self.sources[self.source_field] += data
        if self.current is not None and self.textarea:
            self.current["fields"][self.textarea] += data

    def handle_endtag(self, tag):
        if tag == "small":
            self.source_field = None
        if tag == "form":
            self.current = None
        elif tag == "select":
            self.select = None
        elif tag == "textarea":
            self.textarea = None

    def form(self, action):
        return next(form for form in self.forms if form["action"] == action)


@pytest.fixture
def panel(first_run):
    service, manager = first_run
    application = FastAPI()
    application.state.settings = Settings()
    application.state.deployment = service
    application.include_router(setup_routes.router)
    application.include_router(settings_routes.router)
    with TestClient(application, base_url="http://127.0.0.1:8765") as client:
        yield client, service, manager


def read_form(client, page="/setup", action="/setup/choices", project="demo"):
    response = client.get(page, params={"project": project})
    assert response.status_code == 200, response.text
    return dict(Forms(response.text).form(action)["fields"])


def submit(client, action, fields):
    return client.post(
        action,
        data=fields,
        headers={
            "Origin": "http://127.0.0.1:8765",
            "X-Ddak-Form": "1",
            "Accept": "text/html",
        },
        follow_redirects=False,
    )


def assert_form_error(response, code):
    # 입력 유지·폼 아래 오류 표시는 #32 공통 app.js의 test_form_submit_js가 검증한다.
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers
    error = response.json()["error"]
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert PRIVATE not in response.text


@pytest.mark.parametrize(
    "patch,status",
    [
        ({"version": "not-an-integer"}, 400),
        ({"ai_timeout_s": "not-a-number"}, 400),
        ({"generation_provider": "unknown-fixture-provider"}, 409),
    ],
)
def test_choices_input_errors_return_shared_form_error_without_saving(panel, patch, status):
    client, service, _ = panel
    fields = read_form(client)
    preserved = {
        "generation_model": "operator-model-v2",
        "image_repository": "fixture/operator-image",
        "git_author_name": "Fixture Operator",
        "git_author_email": "operator@example.test",
    }
    response = submit(client, "/setup/choices", {**fields, **preserved, **patch})
    assert response.status_code == status
    assert service.get_project_settings("demo") is None
    assert_form_error(response, "HTTP_400" if status == 400 else "CONFIG_INVALID")


def test_choices_conflict_preserves_saved_identity_and_does_not_partially_save(panel):
    client, service, manager = panel
    manager.save_choices(
        "demo",
        {
            "image_repository": "fixture/original",
            "git_author_name": "Saved Operator",
            "git_author_email": "saved@example.test",
        },
        expected_version=0,
    )
    fields = read_form(client)
    winner = manager.save_choices(
        "demo", {"image_repository": "fixture/winner"}, expected_version=int(fields["version"])
    )
    attempted = {
        "image_repository": "fixture/attempted",
        "generation_model": "attempted-v2",
        "git_author_name": "Attempted Operator",
        "git_author_email": "attempted@example.test",
    }
    response = submit(client, "/setup/choices", {**fields, **attempted})
    assert response.status_code == 409
    assert service.get_project_settings("demo") == winner
    assert_form_error(response, "PRECONDITION_FAILED")


def test_choices_storage_failure_502_returns_shared_form_error_and_hides_exception(panel):
    client, service, _ = panel
    fields = read_form(client)
    # 실제 SQLite 쓰기 장애. save_choices/Store는 교체하지 않는다.
    with service.store.connection() as db:
        db.execute(
            "CREATE TRIGGER fixture_setup_failure BEFORE INSERT ON project_settings "
            "WHEN NEW.project='demo' BEGIN SELECT RAISE(ABORT, '" + PRIVATE + "'); END"
        )
    preserved = {"image_repository": "fixture/keep-me", "generation_model": "operator-v3"}
    response = submit(client, "/setup/choices", {**fields, **preserved})
    assert response.status_code == 502
    assert service.get_project_settings("demo") is None
    assert_form_error(response, "INTERNAL")


@pytest.mark.parametrize("first", ["setup", "settings"])
def test_settings_and_setup_stale_tabs_merge_disjoint_fields(panel, first):
    client, service, _ = panel
    service.save_project_settings(
        "demo",
        {"repo_url": REPO, "watch_branch": "prod", "auto_detect": False},
        updated_by="fixture",
        expected_version=0,
    )
    choices = read_form(client)
    project = read_form(client, "/settings", "/settings")
    assert choices["version"] == project["version"]
    requests = {
        "setup": ("/setup/choices", {**choices, "image_repository": "fixture/changed"}),
        "settings": ("/settings", {**project, "watch_branch": "release"}),
    }
    for name in (first, "settings" if first == "setup" else "setup"):
        response = submit(client, *requests[name])
        assert response.status_code == 303, response.text
    saved = service.get_project_settings("demo")
    assert saved["image_repository"] == "fixture/changed"
    assert saved["watch_branch"] == "release"
    assert saved["repo_url"] == REPO and saved["auto_detect"] is False


@pytest.mark.parametrize("real", [False, True])
def test_two_choices_tabs_do_not_treat_unsaved_effective_prefill_as_an_edit(panel, real):
    client, service, _ = panel
    if real:
        service.onboarding = app._setup_service(
            service, Settings.from_env({"DDAK_ADAPTER_MODE": "real"}), "127.0.0.1"
        )
    initial = service.save_project_settings(
        "demo",
        {"repo_url": REPO, "auto_detect": False},
        updated_by="fixture",
        expected_version=0,
    )
    assert initial.get("ai_timeout_s") is None
    first_tab = read_form(client)
    stale_tab = read_form(client)
    assert first_tab["version"] == stale_tab["version"] == str(initial["version"])
    assert float(stale_tab["ai_timeout_s"]) == (90 if real else 20)
    first_response = submit(client, "/setup/choices", {**first_tab, "ai_timeout_s": "60"})
    assert first_response.status_code == 303, first_response.text
    assert service.get_project_settings("demo")["ai_timeout_s"] == 60
    # 오래된 탭의 timeout은 GET에서 받은 그대로이며 사용자는 이미지 값만 바꿨다.
    second_response = submit(
        client, "/setup/choices", {**stale_tab, "image_repository": "fixture/second-tab"}
    )
    assert second_response.status_code == 303, second_response.text
    saved = service.get_project_settings("demo")
    assert saved["ai_timeout_s"] == 60
    assert saved["image_repository"] == "fixture/second-tab"


def test_product_builder_prefill_submit_preserves_install_plan(panel):
    client, _, manager = panel
    before = manager.build_plan("demo")
    assert before["paths"]["builder"].startswith("ddak-product-")
    assert any(a.get("argv", [])[:3] == ["docker", "buildx", "create"] for a in before["actions"])
    fields = read_form(client)
    response = submit(client, "/setup/choices", fields)
    assert response.status_code == 303, response.text
    after = manager.build_plan("demo")
    assert after["paths"] == before["paths"]
    assert after["actions"] == before["actions"]
    assert after["hash"] == before["hash"]


def test_watcher_handoff_redirect_explains_old_and_new_owner(panel):
    client, service, _ = panel
    service.save_project_settings(
        "old-app",
        {"repo_url": REPO, "watch_branch": "prod", "auto_detect": True},
        updated_by="fixture",
        expected_version=0,
    )
    fields = read_form(client, "/settings", "/settings", "new-app")
    response = submit(client, "/settings", {**fields, "repo_url": REPO, "auto_detect": "on"})
    assert response.status_code == 303, response.text
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert "old-app" in page.text and "new-app" in page.text
    assert "감시" in page.text and ("전환" in page.text or "이전" in page.text)
    assert service.get_project_settings("old-app")["auto_detect"] is False
    assert service.get_project_settings("new-app")["auto_detect"] is True


def test_real_setup_sources_and_values_match_effective_run_settings(panel, monkeypatch):
    client, service, _ = panel
    environment = {"DDAK_ADAPTER_MODE": "real", "DDAK_BUILD_BACKEND": "codebuild"}
    monkeypatch.setenv("DDAK_BUILD_BACKEND", "codebuild")
    manager = app._setup_service(service, Settings.from_env(environment), "127.0.0.1")
    service.onboarding = manager
    before = client.get("/setup?project=demo")
    assert before.status_code == 200
    initial = Forms(before.text)
    assert "기본 파일" in initial.sources["generation_provider"]
    assert "실행환경" in initial.sources["build_backend"]
    assert "설정 필요" in initial.sources["git_author_name"]
    manager.save_choices(
        "demo",
        {"build_backend": "local", "image_repository": "fixture/managed"},
        expected_version=0,
    )
    response = client.get("/setup?project=demo")
    assert response.status_code == 200
    markup = Forms(response.text)
    values = markup.form("/setup/choices")["fields"]
    effective = manager.effective("demo", service.get_project_settings("demo"), manager.vault)
    assert (
        values["generation_provider"] == effective.selected_provider("generation") == "claude-cli"
    )
    assert values["judgment_provider"] == effective.selected_provider("judgment") == "claude-cli"
    assert values["generation_model"] == effective.llm_model == "claude-sonnet-5-5"
    assert values["judgment_model"] == effective.judgment_model == "claude-sonnet-5-5"
    assert float(values["ai_timeout_s"]) == effective.ai_timeout_s == 90
    assert values["build_backend"] == effective.build_backend == "local"
    assert values["image_repository"] == effective.image_repository == "fixture/managed"
    assert "관리 페이지" in markup.sources["build_backend"]
    assert "관리 페이지" in markup.sources["image_repository"]
    assert "기본 파일" in markup.sources["generation_provider"]


def test_reset_to_environment_conflicts_with_stale_different_management_value(panel):
    client, service, _ = panel
    service.onboarding = app._setup_service(
        service,
        Settings.from_env({"DDAK_ADAPTER_MODE": "real", "DDAK_IMAGE_REPOSITORY": "fixture/env"}),
        "127.0.0.1",
    )
    service.save_project_settings(
        "demo", {"image_repository": "fixture/original"}, updated_by="fixture", expected_version=0
    )
    stale = read_form(client)
    reset = read_form(client)
    assert submit(client, "/setup/choices", {**reset, "image_repository": ""}).status_code == 303
    result = submit(client, "/setup/choices", {**stale, "image_repository": "2026gerbera/flaskr"})
    assert result.status_code == 409
    assert service.get_project_settings("demo")["image_repository"] is None


def test_restarted_effective_defaults_use_each_forms_own_baseline(panel):
    client, service, _ = panel
    service.onboarding = app._setup_service(
        service, Settings.from_env({"DDAK_ADAPTER_MODE": "real"}), "127.0.0.1"
    )
    assert read_form(client)["ai_timeout_s"] in {"90", "90.0"}
    service.onboarding = app._setup_service(
        service,
        Settings.from_env({"DDAK_ADAPTER_MODE": "real", "DDAK_AI_TIMEOUT_S": "120"}),
        "127.0.0.1",
    )
    first, second = read_form(client), read_form(client)
    assert float(second["ai_timeout_s"]) == 120
    assert submit(client, "/setup/choices", {**first, "ai_timeout_s": "60"}).status_code == 303
    result = submit(client, "/setup/choices", {**second, "image_repository": "fixture/new"})
    assert result.status_code == 303, result.text
    saved = service.get_project_settings("demo")
    assert saved["ai_timeout_s"] == 60 and saved["image_repository"] == "fixture/new"


def test_settings_display_legacy_effective_watcher_and_preserve_it_on_submit(panel):
    client, service, _ = panel
    service.save_project_settings(
        "demo", {"repo_url": REPO}, updated_by="fixture", expected_version=0
    )
    response = client.get("/settings?project=demo")
    markup = Forms(response.text)
    form = markup.form("/settings")
    toggle = next(attrs for _, attrs, _ in form["controls"] if attrs.get("name") == "auto_detect")
    assert "checked" in toggle
    assert "기본 파일" in markup.sources["auto_detect"]
    assert submit(client, "/settings", form["fields"]).status_code == 303
    assert project_values(service.get_project_settings("demo"))["auto_detect"] is True


@pytest.mark.parametrize("invalid", ["unknown", "other-project", "other-version"])
def test_settings_view_reference_is_bound_to_project_and_version(panel, invalid):
    client, service, _ = panel
    fields = read_form(client)
    if invalid == "unknown":
        fields["settings_view"] = "0" * 64
    elif invalid == "other-project":
        fields["settings_view"] = read_form(client, project="other")["settings_view"]
    else:
        fields["version"] = "1"
    result = submit(client, "/setup/choices", {**fields, "image_repository": "fixture/rejected"})
    assert result.status_code == 409
    assert service.get_project_settings("demo") is None


@pytest.mark.parametrize("existing_owner", [False, True])
def test_first_repository_save_respects_unchecked_auto_detect(panel, existing_owner):
    client, service, _ = panel
    if existing_owner:
        service.save_project_settings(
            "old-app",
            {"repo_url": REPO, "auto_detect": True},
            updated_by="fixture",
            expected_version=0,
        )
    fields = read_form(client, page="/settings", action="/settings")
    assert "auto_detect" not in fields
    result = submit(client, "/settings", {**fields, "repo_url": REPO})
    assert result.status_code == 303
    assert project_values(service.get_project_settings("demo"))["auto_detect"] is False
    assert "transferred" not in result.headers["location"]
    if existing_owner:
        assert service.get_project_settings("old-app")["auto_detect"] is True
