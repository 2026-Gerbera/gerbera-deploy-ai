"""fix11 독립 리뷰의 5개 회귀: 실제 조립/guard와 가짜 외부 경계만 사용한다."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import socket
import stat
import subprocess
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ddak import app as assembly
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.project_settings import ProjectSettings
from ddak.core.tool_paths import scanner_binary
from ddak.executor.service import DeploymentService
from ddak.plan.intake.policy import FetchPolicy
from ddak.plan.intake.watch import WatchTarget
from ddak.web.routes import setup
from ddak.web.security import CSRF_COOKIE
from tests.unit.test_deployment_service import plan as fixture_plan

PROJECT = "setup-review"
BASE = "http://127.0.0.1:8765"
MIGRATION_MARKER = "fixture-migration-credential"
MIGRATION_URL = f"mysql+pymysql://fixture_migrator:{MIGRATION_MARKER}@db.invalid:3306/app"
RUNTIME_URL = "mysql+pymysql://fixture_runtime:fixture-app-credential@db.invalid:3306/app"


@pytest.fixture(autouse=True)
def isolated_external_boundaries(monkeypatch):
    """실수로 시도한 외부 호출도, production이 예외를 삼켜도 테스트를 실패시킨다."""
    for key in tuple(os.environ):
        if key.startswith("DDAK_"):
            monkeypatch.delenv(key)
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append("external-call")
        raise AssertionError("외부 실행은 이 회귀 테스트에서 금지된다")

    async def forbidden_async(*args, **kwargs):
        forbidden()

    for module, name in (
        (subprocess, "run"),
        (subprocess, "Popen"),
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (urllib.request, "urlopen"),
        (assembly, "test_provider_connection"),
        (assembly, "preflight_inventory"),
        (assembly, "resolve_head"),
    ):
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_async)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden_async)
    monkeypatch.setattr(assembly, "BuildSetup", FakeBuildSetup)
    yield
    assert not attempted, "외부 호출을 시도했다(실행은 차단됨)"


class FakeBuildSetup:
    def __init__(self, root):
        self.root = Path(root)
        self.docker_config = self.root / "docker"
        self.builder_name = "fixture-builder"
        self.tool_dir = self.root / "tools" / "bin"

    def plan(self):
        return {
            "hash": "a" * 64,
            "actions": {"scanner": {"action": "fixture only", "required": True}},
            "paths": {"tool_dir": str(self.tool_dir)},
        }

    def probe(self):
        return {"status": "blocked", "detail": "fixture build unavailable"}

    def login(self, username, token):
        return {"status": "blocked", "detail": "fixture docker unavailable"}


class MemoryService:
    """DB/저장소 경계만 대체. 설정 해석·coordinator·guard는 실제 코드다."""

    def __init__(self, root, saved=None):
        self.root = root.resolve() / "state"
        self.root.mkdir()
        self.saved = {"version": 1, **(saved or {})}
        self.registry = None
        self.repository_factory = None
        self.prepared = []
        self.failures = []
        self.approvals = []
        self.acquired = []
        self.store = SimpleNamespace(
            environments=lambda project: {},
            approve=self.approvals.extend,
            acquire=self.acquire,
        )
        self.onboarding = None
        self.stages = []

    preparation_stage = DeploymentService.preparation_stage

    def record_stage(self, run_id, name, elapsed_ms, status):
        self.stages.append((run_id, name, elapsed_ms, status))

    def resolve_project(self, project):
        assert project == PROJECT
        return project

    def get_project_settings(self, project):
        self.resolve_project(project)
        return copy.deepcopy(self.saved)

    def save_project_settings(self, project, data, *, updated_by, expected_version):
        self.resolve_project(project)
        assert expected_version == self.saved["version"]
        ProjectSettings.model_validate({**self.saved, **data, "version": None}, extra="ignore")
        self.saved.update(data)
        self.saved["version"] += 1
        return copy.deepcopy(self.saved)

    async def begin_preparation(self, project, run_id):
        self.resolve_project(project)

    def end_preparation(self, project, run_id):
        self.resolve_project(project)

    def get_platform_outputs(self, project, mode):
        return {}

    def record_preparation_failure(self, run_id, project, error, **kwargs):
        self.failures.append(error)
        return False

    def prepare(self, plan, context, source, **kwargs):
        self.prepared.append(SimpleNamespace(plan=plan, context=context, source=source))

    def acquire(self, project, run_id, **kwargs):
        self.acquired.append(run_id)
        return "fixture-lock"


def coordinator(tmp_path, saved=None, environ=None):
    service = MemoryService(tmp_path, saved)
    settings = Settings.from_env(environ or {})
    service.onboarding = assembly._setup_service(service, settings, "local")
    return service, settings


def fake_planning(monkeypatch, tmp_path):
    source = tmp_path / "fixture-source"
    source.mkdir()
    (source / "app.py").write_text("VERSION = 1\n")
    calls = []

    def plan(request, *, run_id, settings, platform, **kwargs):
        calls.append({"settings": settings, "platform": platform, "scanner": scanner_binary()})
        return SimpleNamespace(
            plan=fixture_plan(run_id).model_copy(update={"project": PROJECT}),
            context=RunContext(run_id, project=PROJECT),
            source=source,
            facts=None,
        )

    async def infra(*args):
        return {}, None

    monkeypatch.setattr(assembly, "plan_deployment", plan)
    monkeypatch.setattr(assembly, "get_jev_client", lambda settings: object())
    monkeypatch.setattr(assembly, "missing_track_tools", lambda *args: {})
    monkeypatch.setattr(assembly, "_infra_approval", infra)
    return calls


async def prepare(service, settings, tmp_path):
    return await assembly._prepare_commit(
        service,
        settings,
        WatchTarget(PROJECT, "https://github.com/fixture/app.git"),
        "a" * 40,
        policy=FetchPolicy(root=tmp_path / "intake"),
    )


def execution_boundary(service, prepared):
    executor = object.__new__(DeploymentService)
    executor.onboarding = service.onboarding
    executor.store = service.store
    executor._tasks, executor._tokens = {}, {}
    executor._load_prepared = lambda run_id: prepared
    executor._check_approval = lambda prepared: None
    executor._check_meta = lambda prepared: None

    async def execute(prepared, token):
        return scanner_binary()

    executor._execute = execute
    return executor


# 1. 저장값 None/미지정이어도 실제 local backend의 red가 준비·승인·실행을 막는다.
@pytest.mark.anyio
@pytest.mark.parametrize("saved", [{}, {"build_backend": None}])
@pytest.mark.parametrize("kind", ["build", "docker"])
@pytest.mark.parametrize("entry", ["prepare", "approve", "start"])
async def test_effective_local_red_blocks_all_entrypoints(
    tmp_path, monkeypatch, saved, kind, entry
):
    monkeypatch.setenv("DDAK_BUILD_BACKEND", "local")
    service, settings = coordinator(tmp_path, saved, {"DDAK_BUILD_BACKEND": "local"})
    setup_service = service.onboarding
    if kind == "build":
        setup_service.probe(PROJECT, kind)
    else:
        setup_service.login_docker(PROJECT, "fixture-user", "fixture-token")
    view = setup_service.view(PROJECT)
    assert view["settings"]["build_backend"] == "local"
    assert next(row for row in view["checklist"] if row["id"] == kind)["status"] == "red"
    with pytest.raises(DdakToolError) as readiness:
        setup_service.require_ready(PROJECT, "onprem")
    assert readiness.value.code is ErrorCode.PRECONDITION_FAILED

    if entry == "prepare":
        calls = fake_planning(monkeypatch, tmp_path)
        await prepare(service, settings, tmp_path)
        assert not calls and not service.prepared
        assert len(service.failures) == 1
        assert service.failures[0].code is ErrorCode.PRECONDITION_FAILED
    else:
        prepared = SimpleNamespace(
            plan=fixture_plan().model_copy(update={"project": PROJECT}),
            context=RunContext("run-2", project=PROJECT, targets="onprem"),
            requirements={},
        )
        executor = execution_boundary(service, prepared)
        with pytest.raises(DdakToolError) as blocked:
            if entry == "approve":
                executor.approve("run-2", approver="fixture-operator")
            else:
                executor.start("run-2")
        assert blocked.value.code is ErrorCode.PRECONDITION_FAILED
        assert not service.approvals and not service.acquired and not executor._tasks


def test_effective_codebuild_does_not_require_local_build_or_docker(tmp_path):
    service, _ = coordinator(tmp_path)
    service.onboarding.probe(PROJECT, "build")
    service.onboarding.login_docker(PROJECT, "fixture-user", "fixture-token")
    assert service.onboarding.view(PROJECT)["settings"]["build_backend"] == "codebuild"
    service.onboarding.require_ready(PROJECT, "onprem")


# 2. 준비 단계에서 선택한 관리 scanner 경로가 CodeBuild 실행 task에도 전달된다.
@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["codebuild", "local"])
async def test_preparation_carries_managed_scanner_to_execution(tmp_path, monkeypatch, backend):
    service, settings = coordinator(tmp_path, {"build_backend": backend})
    tool_dir = Path(service.onboarding.build_context(PROJECT)["tool_dir"])
    tool_dir.mkdir(parents=True)
    (tool_dir / "gitleaks").write_text("fixture marker; never executed\n")
    calls = fake_planning(monkeypatch, tmp_path)
    outside = scanner_binary()
    run_id = await prepare(service, settings, tmp_path)
    assert not service.failures and len(service.prepared) == 1
    expected = str(tool_dir / "gitleaks")
    assert calls[0]["scanner"] == expected
    assert scanner_binary() == outside
    prepared = service.prepared[0]
    assert prepared.context.build_backend == backend
    executor = execution_boundary(service, prepared)
    assert await executor.start(run_id) == expected
    assert scanner_binary() == outside


# 3. 공개 migration API/웹을 통해 기본 인벤토리의 비밀 파일을 별도로 완성한다.
def register_runtime(service):
    service.onboarding.register_inventory(
        PROJECT,
        {
            "mode": "container",
            "tiers": {
                "was": {
                    "name": "fixture-was",
                    "kind": "python_http",
                    "platform": "linux/arm64",
                }
            },
        },
    )
    service.onboarding.save_env(PROJECT, "DATABASE_URL", RUNTIME_URL)
    service.onboarding.save_env(PROJECT, "APP_SETTING", "fixture-setting")
    return service.onboarding.runtime_env(PROJECT).read_bytes()


def assert_private_migration(service, previous_runtime):
    view = service.onboarding.view(PROJECT)
    was = view["inventory"]["tiers"]["was"]
    runtime = Path(was["env_file"])
    migration = Path(was["migration_env_file"])
    assert runtime != migration
    assert runtime.read_bytes() == previous_runtime
    runtime_values = dict(line.split("=", 1) for line in runtime.read_text().splitlines())
    assert runtime_values["DATABASE_URL"] == RUNTIME_URL
    assert runtime_values["APP_SETTING"] == "fixture-setting"
    assert all("MIGRATOR" not in key for key in runtime_values)
    assert MIGRATION_MARKER not in runtime.read_text()
    migration_values = dict(line.split("=", 1) for line in migration.read_text().splitlines())
    assert migration_values["DATABASE_URL_MIGRATOR"] == MIGRATION_URL
    assert migration_values["DATABASE_URL"] == MIGRATION_URL
    assert stat.S_IMODE(migration.stat().st_mode) == 0o600
    assert stat.S_IMODE(migration.parent.stat().st_mode) == 0o700
    assert set(view["env_keys"]) == {"DATABASE_URL", "APP_SETTING"}
    assert MIGRATION_URL not in json.dumps(view)
    assert MIGRATION_MARKER not in json.dumps(view)
    assert MIGRATION_MARKER not in json.dumps(service.saved)


def test_save_migration_url_public_api_is_private_and_completes_default_inventory(
    tmp_path, caplog, capsys
):
    service, _ = coordinator(tmp_path)
    previous = register_runtime(service)
    result = service.onboarding.save_migration_url(PROJECT, MIGRATION_URL)
    assert MIGRATION_MARKER not in repr(result)
    assert_private_migration(service, previous)
    public_output = caplog.text + str(capsys.readouterr())
    assert MIGRATION_URL not in public_output and MIGRATION_MARKER not in public_output


class MigrationForm(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.in_migration = False
        self.inputs = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.in_migration = attrs.get("action") == "/setup/migration"
        if tag == "input" and self.in_migration:
            self.inputs.append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_migration = False


def web_app(service):
    app = FastAPI()
    app.state.settings = SimpleNamespace(admin_port=8765)
    app.state.deployment = service
    app.include_router(setup.router)
    return app


def test_migration_password_form_uses_actual_coordinator_without_echo(tmp_path):
    service, _ = coordinator(tmp_path)
    previous = register_runtime(service)
    with TestClient(web_app(service), base_url=BASE) as client:
        page = client.get("/setup", params={"project": PROJECT})
        assert page.status_code == 200
        inputs = MigrationForm(page.text).inputs
        password = next(inp for inp in inputs if inp.get("name") == "value")
        assert password["type"] == "password" and not password.get("value")
        response = client.post(
            "/setup/migration",
            data={
                "project": PROJECT,
                "csrf_token": client.cookies[CSRF_COOKIE],
                "value": MIGRATION_URL,
            },
            headers={"Origin": BASE},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/setup?project={PROJECT}"
        after = client.get(response.headers["location"])
        assert after.status_code == 200 and after.headers["cache-control"] == "no-store"
        public = response.text + after.text + str(response.headers)
        assert MIGRATION_URL not in public and MIGRATION_MARKER not in public
        assert not next(
            inp for inp in MigrationForm(after.text).inputs if inp.get("name") == "value"
        ).get("value")
    assert_private_migration(service, previous)


@pytest.mark.parametrize("denied,status", [("csrf", 403), ("origin", 403), ("host", 400)])
def test_migration_requires_safe_post_before_public_api(tmp_path, monkeypatch, denied, status):
    service, _ = coordinator(tmp_path)
    calls = []
    monkeypatch.setattr(service.onboarding, "save_migration_url", lambda *args: calls.append(args))
    with TestClient(web_app(service), base_url=BASE) as client:
        assert client.get("/setup", params={"project": PROJECT}).status_code == 200
        data = {
            "project": PROJECT,
            "csrf_token": client.cookies[CSRF_COOKIE],
            "value": MIGRATION_URL,
        }
        headers = {"Origin": BASE}
        if denied == "csrf":
            data["csrf_token"] = "fixture-wrong-token"
        elif denied == "origin":
            headers["Origin"] = "http://fixture.invalid"
        else:
            headers["Host"] = "fixture.invalid"
        response = client.post("/setup/migration", data=data, headers=headers)
        assert response.status_code == status and not calls
        assert MIGRATION_MARKER not in response.text


@pytest.mark.parametrize("exception,status", [(ValueError, 400), (RuntimeError, 502)])
def test_migration_callback_error_never_echoes_url(tmp_path, monkeypatch, exception, status):
    service, _ = coordinator(tmp_path)

    def failing(project, value):
        assert project == PROJECT and value == MIGRATION_URL
        raise exception(value)

    monkeypatch.setattr(service.onboarding, "save_migration_url", failing)
    with TestClient(web_app(service), base_url=BASE) as client:
        assert client.get("/setup", params={"project": PROJECT}).status_code == 200
        response = client.post(
            "/setup/migration",
            data={
                "project": PROJECT,
                "csrf_token": client.cookies[CSRF_COOKIE],
                "value": MIGRATION_URL,
            },
            headers={"Origin": BASE},
        )
        assert response.status_code == status
        assert MIGRATION_URL not in response.text and MIGRATION_MARKER not in response.text


# 4. UI 검사에서 green으로 확인한 인벤토리와 계획 입력의 파일 선택은 일치해야 한다.
@pytest.mark.anyio
@pytest.mark.parametrize("environment_override", [False, True])
async def test_inventory_probe_view_and_planning_choose_same_file(
    tmp_path, monkeypatch, environment_override
):
    saved_path, env_path = tmp_path / "ui-inventory.json", tmp_path / "env-inventory.json"
    if environment_override:
        monkeypatch.setenv("DDAK_ONPREM_INVENTORY", str(env_path))
    probed = []

    def read_inventory(path):
        return {"mode": "container", "tiers": {}, "fixture_source": str(path)}

    def preflight(inventory, *, project):
        assert project == PROJECT
        probed.append(inventory["fixture_source"])
        return {"passed": True}

    monkeypatch.setattr(assembly, "load_inventory", read_inventory)
    monkeypatch.setattr(assembly, "preflight_inventory", preflight)
    service, settings = coordinator(tmp_path, {"inventory_path": str(saved_path)})
    result = service.onboarding.probe(PROJECT, "inventory")
    assert result["status"] == "green" and len(probed) == 1
    visible = service.onboarding.view(PROJECT)["inventory"]["fixture_source"]
    calls = fake_planning(monkeypatch, tmp_path)
    await prepare(service, settings, tmp_path)
    assert not service.failures and len(service.prepared) == 1
    expected = str(env_path if environment_override else saved_path)
    assert probed[0] == visible == calls[0]["platform"]["onprem"]["fixture_source"] == expected
    assert service.prepared[0].context.platform["onprem"]["fixture_source"] == expected


# 5. 명시적 환경 provider/model이 저장값·provider 기본 모델에 덮이지 않는다.
@pytest.mark.parametrize(
    "environ,saved,field,expected",
    [
        (
            {"DDAK_LLM_PROVIDER": "groq"},
            {"generation_provider": "claude-api"},
            "llm_provider",
            "groq",
        ),
        (
            {"DDAK_JUDGMENT_PROVIDER": "groq"},
            {"judgment_provider": "claude-api"},
            "judgment_provider",
            "groq",
        ),
        (
            {"DDAK_JUDGMENT_MODEL": "environment-model"},
            {"judgment_provider": "claude-api"},
            "judgment_model",
            "environment-model",
        ),
        (
            {"DDAK_LLM_MODEL": "environment-model"},
            {"generation_provider": "claude-api"},
            "llm_model",
            "environment-model",
        ),
        (
            {},
            {"generation_provider": "claude-api", "generation_model": "saved-model"},
            "llm_model",
            "saved-model",
        ),
        (
            {},
            {"judgment_provider": "claude-api", "judgment_model": "saved-model"},
            "judgment_model",
            "saved-model",
        ),
    ],
)
def test_explicit_provider_and_model_environment_wins(
    tmp_path, monkeypatch, environ, saved, field, expected
):
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    service, base = coordinator(tmp_path, saved, environ)
    effective = service.onboarding.effective(PROJECT, service.saved, service.onboarding.vault)
    assert getattr(effective, field) == expected
    public_field = {"llm_provider": "generation_provider", "llm_model": "generation_model"}.get(
        field, field
    )
    assert service.onboarding.view(PROJECT)["settings"][public_field] == expected
    assert base == Settings.from_env(environ), "공유 base 설정은 바꾸지 않아야 한다"
