"""초기 설정 웹을 가짜 coordinator로 검증한다. 외부 실행은 하지 않는다."""

from __future__ import annotations

import asyncio
import copy
import json
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.web.routes import setup

SECRET = "  fake-local-" + "credential<&>  "
HASH = "a" * 64
POSTS = [
    ("choices", {"generation_provider": "custom", "generation_model": "custom-v2", "version": "7"}),
    ("key", {"provider_id": "custom", "value": SECRET}),
    ("key-delete", {"provider_id": "custom"}),
    ("test", {"provider_id": "custom"}),
    ("inventory", {"inventory": '{"mode":"container","tiers":{}}'}),
    ("env", {"key": "DATABASE_URL", "value": SECRET}),
    ("build-apply", {"plan_hash": HASH}),
    ("docker", {"username": "local-user", "token": SECRET}),
    ("probe", {"kind": "ai"}),
]


class FakeCoordinator:
    def __init__(self):
        self.calls = []
        self.loop_calls = []
        self.errors = {}
        self.private = {}
        self.data = {
            "settings": {
                "version": 7,
                "generation_provider": "custom",
                "generation_model": "custom-v2",
                "judgment_provider": "judge",
                "judgment_model": None,
                "llm_effort": "medium",
                "build_backend": "local",
                "image_repository": "local-user/app",
            },
            "providers": [
                {
                    "id": "custom",
                    "label": "Custom API",
                    "kind": "api",
                    "roles": ["generation", "judgment"],
                    "auth": "api_key",
                    "default_model": "custom-v1",
                    "models": ["custom-v1", "custom-v2"],
                    "key_name": "CUSTOM_API_KEY",
                },
                {
                    "id": "judge",
                    "label": "Judge API",
                    "kind": "api",
                    "roles": ["judgment"],
                    "auth": "api_key",
                    "default_model": "judge-v1",
                    "models": ["judge-v1"],
                    "key_name": "JUDGE_API_KEY",
                },
                {
                    "id": "local-cli",
                    "label": "Local CLI",
                    "kind": "cli",
                    "roles": ["generation"],
                    "auth": "session",
                    "default_model": "local-v1",
                    "models": [],
                    "key_name": None,
                },
                {
                    "id": "jev",
                    "label": "Jev",
                    "kind": "jev",
                    "roles": [],
                    "auth": "none",
                    "default_model": None,
                    "models": [],
                    "key_name": None,
                },
            ],
            "states": {"custom": {"status": "gray", "detail": "실제 연결 확인 전"}},
            "keys": {"custom": True},
            "checklist": [
                {
                    "id": "repository",
                    "label": "저장소",
                    "status": "red",
                    "detail": "push 권한 없음",
                },
                {"id": "ai", "label": "AI", "status": "gray", "detail": "실제 연결 확인 전"},
            ],
            "inventory": {
                "mode": "container",
                "tiers": {
                    "was": {
                        "name": "app-was",
                        "platform": "linux/arm64",
                        "public_env": {"DB_HOST": "db"},
                    }
                },
            },
            "env_keys": ["DATABASE_URL"],
            "build_plan": {
                "hash": HASH,
                "actions": {"builder": {"action": "prepare builder", "required": True}},
                "paths": {"tool_dir": "/local/tools"},
            },
        }

    def _record(self, name, *args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            self.loop_calls.append(name)
        self.calls.append((name, args, kwargs))
        if name in self.errors:
            raise self.errors[name]

    def view(self, project):
        self._record("view", project)
        return copy.deepcopy(self.data)

    def save_choices(self, project, data, *, expected_version):
        self._record("save_choices", project, data, expected_version=expected_version)

    def save_key(self, project, provider_id, value):
        self._record("save_key", project, provider_id, value)
        self.private[provider_id] = value
        self.data["keys"][provider_id] = True

    def delete_key(self, project, provider_id):
        self._record("delete_key", project, provider_id)
        self.private.pop(provider_id, None)
        self.data["keys"][provider_id] = False

    def test_provider(self, project, provider_id):
        self._record("test_provider", project, provider_id)
        self.data["states"][provider_id] = {
            "status": "green",
            "detail": "실제 콜백 확인",
            "verified_at": "2026-10-03T10:00:00Z",
        }

    def register_inventory(self, project, data):
        self._record("register_inventory", project, data)
        self.data["inventory"] = data

    def save_env(self, project, key, value):
        self._record("save_env", project, key, value)
        self.private[key] = value

    def apply_build(self, project, plan_hash):
        self._record("apply_build", project, plan_hash)
        if plan_hash != HASH:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "오래된 계획")

    def login_docker(self, project, username, token):
        self._record("login_docker", project, username, token)
        self.private["docker"] = token
        return {"token": token}

    def probe(self, project, kind):
        self._record("probe", project, kind)


class Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.selects = {}
        self.options = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == "select":
            self.options = []
            self.selects[attrs["name"]] = self.options
        if tag == "option":
            self.options.append(attrs)

    def handle_endtag(self, tag):
        if tag == "select":
            self.options = []


@pytest.fixture
def panel(monkeypatch):
    monkeypatch.setenv("DDAK_WATCH_PROJECT", "demo")
    coordinator = FakeCoordinator()
    app = FastAPI()
    app.state.settings = SimpleNamespace(admin_port=8765)
    app.state.deployment = SimpleNamespace(
        onboarding=coordinator, resolve_project=lambda name: name
    )
    app.include_router(setup.router)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/setup?project=other")
        assert response.status_code == 200
        coordinator.calls.clear()
        yield client, coordinator


def post(client, action, data):
    return client.post(
        "/setup/" + action,
        data={"csrf_token": client.cookies["ddak_csrf"], "project": "other", **data},
        headers={"Origin": "http://127.0.0.1:8765"},
        follow_redirects=False,
    )


def test_metadata_custom_provider_roles_models_and_gray_key(panel):
    client, coordinator = panel
    response = client.get("/setup?project=other")
    html = response.text
    markup = Markup(html)
    assert response.headers["cache-control"] == "no-store"
    assert coordinator.calls == [("view", ("other",), {})]
    assert "Custom API" in html and "Judge API" in html and "Jev" in html
    assert {x["value"] for x in markup.selects["generation_provider"]} == {
        "",
        "custom",
        "local-cli",
    }
    assert {x["value"] for x in markup.selects["judgment_provider"]} == {"", "custom", "judge"}
    for name, selected in [("generation_provider", "custom"), ("judgment_provider", "judge")]:
        assert next(x["value"] for x in markup.selects[name] if "selected" in x) == selected
    inputs = [attrs for tag, attrs in markup.tags if tag == "input"]
    assert any(x.get("name") == "judgment_model" and x.get("value") == "judge-v1" for x in inputs)
    assert any(
        x.get("name") == "generation_model"
        and x.get("list") == "generation-models"
        and x.get("type") == "text"
        for x in inputs
    )
    assert "custom-v2" in html and "judge-v1" in html
    assert 'data-status="green"' not in html
    assert 'data-status="gray"' in html and "키 등록됨" in html
    assert "push 권한 없음" in html and 'href="/settings?project=other"' in html
    assert "필요 도구 설치" in html and HASH in html and "prepare builder" in html
    assert "DB_HOST" in html and "DATABASE_URL" in html
    assert any(tag == "textarea" and attrs.get("name") == "inventory" for tag, attrs in markup.tags)
    assert any(tag == "button" and "disabled" in attrs for tag, attrs in markup.tags)
    assert all("value" not in x for x in inputs if x.get("type") == "password")
    assert not coordinator.loop_calls


@pytest.mark.parametrize("action,data", POSTS)
@pytest.mark.parametrize(
    "failure", ["missing_token", "wrong_token", "missing_origin", "bad_origin", "bad_host"]
)
def test_every_mutation_requires_csrf_origin_and_host(panel, action, data, failure):
    client, coordinator = panel
    form = {"project": "other", "csrf_token": client.cookies["ddak_csrf"], **data}
    headers = {"Origin": "http://127.0.0.1:8765"}
    if failure == "missing_token":
        form.pop("csrf_token")
    elif failure == "wrong_token":
        form["csrf_token"] = "wrong"
    elif failure == "missing_origin":
        headers.clear()
    elif failure == "bad_origin":
        headers["Origin"] = "http://external.example"
    else:
        headers["Host"] = "external.example:8765"
    response = client.post("/setup/" + action, data=form, headers=headers, follow_redirects=False)
    assert response.status_code == (400 if failure == "bad_host" else 403)
    assert coordinator.calls == []
    assert SECRET not in response.text
    assert "fake-local" not in response.text


@pytest.mark.parametrize("action,data", POSTS)
def test_mutations_dispatch_then_redirect_without_secret(panel, action, data):
    client, coordinator = panel
    response = post(client, action, data)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup?project=other"
    assert len(coordinator.calls) == 1
    assert coordinator.calls[0][1][0] == "other"
    assert not coordinator.loop_calls
    assert SECRET not in response.text and SECRET not in str(response.headers)
    assert "fake-local" not in response.text and "fake-local" not in str(response.headers)


def test_choices_exact_nonsecret_fields_and_version(panel):
    client, coordinator = panel
    data = {
        "version": "7",
        "generation_provider": "custom",
        "generation_model": "custom-v2",
        "judgment_provider": "judge",
        "judgment_model": "judge-v1",
        "llm_effort": "medium",
        "build_backend": "local",
        "image_repository": " local-user/app ",
        "value": SECRET,
        "unexpected": "ignored",
    }
    assert post(client, "choices", data).status_code == 303
    assert coordinator.calls == [
        (
            "save_choices",
            (
                "other",
                {
                    key: data[key].strip()
                    for key in (
                        "generation_provider",
                        "generation_model",
                        "judgment_provider",
                        "judgment_model",
                        "llm_effort",
                        "build_backend",
                        "image_repository",
                    )
                },
            ),
            {"expected_version": 7},
        )
    ]


@pytest.mark.parametrize("action,field", [("key", "value"), ("env", "value"), ("docker", "token")])
def test_secret_whitespace_preserved_and_never_repopulated(panel, action, field):
    client, coordinator = panel
    payload = dict(next(data for name, data in POSTS if name == action))
    payload[field] = SECRET
    assert post(client, action, payload).status_code == 303
    assert coordinator.calls[0][1][-1] == SECRET
    assert SECRET in coordinator.private.values()
    html = client.get("/setup?project=other").text
    assert SECRET not in html and "fake-local" not in html


@pytest.mark.parametrize(
    "error",
    [ValueError(SECRET), RuntimeError(SECRET), DdakToolError(ErrorCode.CONFIG_INVALID, SECRET)],
)
@pytest.mark.parametrize("action", ["key", "env", "docker", "inventory", "test", "probe"])
def test_exception_messages_never_echo_secret(panel, error, action):
    client, coordinator = panel
    methods = {
        "key": "save_key",
        "env": "save_env",
        "docker": "login_docker",
        "inventory": "register_inventory",
        "test": "test_provider",
        "probe": "probe",
    }
    coordinator.errors[methods[action]] = error
    payload = next(data for name, data in POSTS if name == action)
    response = post(client, action, payload)
    assert response.status_code == (
        409 if isinstance(error, DdakToolError) else 400 if isinstance(error, ValueError) else 502
    )
    assert SECRET not in response.text
    assert "fake-local" not in response.text


def test_custom_and_judgment_only_provider_tests_use_metadata_id(panel):
    client, coordinator = panel
    for ident in ["custom", "judge"]:
        assert post(client, "test", {"provider_id": ident}).status_code == 303
        assert coordinator.calls[-1] == ("test_provider", ("other", ident), {})
        html = client.get("/setup?project=other").text
        assert 'data-status="green"' in html
        assert "실제 콜백 확인" in html and "2026-10-03T10:00:00Z" in html


@pytest.mark.parametrize("kind", ["ai", "inventory", "repository", "build", "docker"])
def test_probes_and_get_never_apply_build(panel, kind):
    client, coordinator = panel
    client.get("/setup?project=other")
    assert post(client, "probe", {"kind": kind}).status_code == 303
    assert coordinator.calls[-1] == ("probe", ("other", kind), {})
    assert all(name != "apply_build" for name, _, _ in coordinator.calls)


def test_build_hash_forwarded_only_on_approval_and_stale_rejected(panel):
    client, coordinator = panel
    assert post(client, "build-apply", {"plan_hash": ""}).status_code == 400
    assert coordinator.calls == []
    assert post(client, "build-apply", {"plan_hash": "old"}).status_code == 409
    assert coordinator.calls == [("apply_build", ("other", "old"), {})]
    assert post(client, "build-apply", {"plan_hash": HASH}).status_code == 303
    assert coordinator.calls[-1] == ("apply_build", ("other", HASH), {})


def test_inventory_public_env_is_passed_as_dict(panel):
    client, coordinator = panel
    data = coordinator.data["inventory"]
    assert post(client, "inventory", {"inventory": json.dumps(data)}).status_code == 303
    assert coordinator.calls == [("register_inventory", ("other", data), {})]


@pytest.mark.parametrize("raw", ["[]", "null", "42", "{", SECRET])
def test_bad_inventory_never_reaches_service_or_echoes_input(panel, raw):
    client, coordinator = panel
    response = post(client, "inventory", {"inventory": raw})
    assert response.status_code == 400
    assert coordinator.calls == []
    assert SECRET not in response.text


def test_missing_service_and_invalid_selection_inputs_fail_closed(panel):
    client, coordinator = panel
    assert post(client, "probe", {"kind": "unknown"}).status_code == 400
    assert post(client, "choices", {"version": SECRET}).status_code == 400
    assert client.get("/setup?project=../outside").status_code == 400
    assert coordinator.calls == []
    client.app.state.deployment.onboarding = None
    assert client.get("/setup?project=other").status_code == 503


def test_default_project_and_deleted_key_remain_gray(panel):
    client, coordinator = panel
    response = client.get("/setup")
    assert response.status_code == 200
    assert coordinator.calls[-1] == ("view", ("demo",), {})
    assert post(client, "key-delete", {"provider_id": "custom"}).status_code == 303
    html = client.get("/setup?project=other").text
    assert "키 미등록" in html and 'data-status="green"' not in html


def test_registry_and_inventory_content_is_html_escaped(panel):
    client, coordinator = panel
    markup = '<script>alert("fixture")</script>'
    provider = coordinator.data["providers"][0]
    provider["label"] = markup
    provider["models"] = [markup]
    coordinator.data["settings"]["generation_model"] = markup
    coordinator.data["inventory"]["tiers"]["was"]["public_env"]["DB_HOST"] = "</textarea>" + markup
    html = client.get("/setup?project=other").text
    assert markup not in html
    assert "&lt;script&gt;" in html
    assert "\\u003c/textarea\\u003e" in html
