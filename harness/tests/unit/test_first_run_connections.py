"""설정·조립·기록 왕복. 외부 자격증명 및 서비스는 사용하지 않는다."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ddak import app
from ddak.core.config import AdapterMode, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.defaults import load_aws_defaults, load_defaults
from ddak.core.git_credentials import save_token
from ddak.core.private_values import SecretVault
from ddak.core.redact import redact_obj
from ddak.plan.intake import FetchPolicy, WatchTarget
from tests.unit.test_deployment_service import plan
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_first_run import REPO
from tests.unit.test_first_run import first_run as first_run
from tests.unit.test_first_run_web import Forms, read_form, submit
from tests.unit.test_first_run_web import panel as panel


def test_aws_defaults_and_management_override_environment(tmp_path, monkeypatch):
    assert load_defaults()["aws_profile"] == "g"
    assert load_aws_defaults()["expected_account_id"] == "458781646776"
    base = Settings.from_env({"DDAK_ADAPTER_MODE": "real", "AWS_PROFILE": "other"})
    assert base.aws_profile == "other"
    configured = app._effective_project_settings(
        base,
        "demo",
        {"aws_profile": "team"},
        SecretVault(tmp_path / "private"),
        cli_host="127.0.0.1",
        check_local=False,
    )
    assert configured.aws_profile == "team"
    assert configured.aws_expected_account_id == "458781646776"
    assert configured.setting_sources["aws_profile"] == "관리 페이지"
    assert Settings.from_env({"DDAK_ADAPTER_MODE": "real"}).aws_profile == "g"


@pytest.mark.parametrize("page,action", [("/setup", "/setup/choices"), ("/settings", "/settings")])
def test_aws_profile_prefill_save_roundtrip(panel, page, action):
    client, service, _ = panel
    response = client.get(page, params={"project": "demo"})
    assert Forms(response.text).form(action)["fields"]["aws_profile"] == "g"
    assert "기본 파일" in Forms(response.text).sources["aws_profile"]
    assert "458781646776" in response.text
    form = read_form(client, page=page, action=action)
    form.update(aws_profile="team-demo")
    if page == "/settings":
        form["repo_url"] = REPO
    assert submit(client, action, form).status_code == 303
    assert service.get_project_settings("demo")["aws_profile"] == "team-demo"
    response = client.get(page, params={"project": "demo"})
    assert Forms(response.text).form(action)["fields"]["aws_profile"] == "team-demo"
    assert "관리 페이지" in Forms(response.text).sources["aws_profile"]


def test_settings_displays_effective_environment_profile(panel):
    client, _, _ = panel
    client.app.state.settings = Settings(aws_profile="environment-team")
    response = client.get("/settings?project=demo")
    assert Forms(response.text).form("/settings")["fields"]["aws_profile"] == "environment-team"
    assert "실행환경" in Forms(response.text).sources["aws_profile"]


@pytest.mark.parametrize("managed", [False, True])
def test_repository_probe_records_auth_source_without_secret(panel, monkeypatch, managed):
    _, service, _ = panel
    service.save_project_settings(
        "demo",
        {"repo_url": REPO, "git_author_name": "Operator", "git_author_email": "op@example.test"},
        updated_by="fixture",
        expected_version=0,
    )
    secret = "fixture-only-push-value"
    if managed:
        save_token(service.root / "private", "demo", REPO, secret)
    calls = []
    monkeypatch.setattr(
        service,
        "connect_repository",
        lambda ctx: SimpleNamespace(check_push_access=lambda branch: calls.append(branch)),
    )
    settings = Settings(
        adapter_mode=AdapterMode.REAL, llm_provider="replay", judgment_provider="replay"
    )
    setup = app._setup_service(service, settings, "127.0.0.1")
    result = setup.probe("demo", "repository")
    assert result["status"] == "green"
    expected = "관리 페이지 토큰" if managed else "머신 Git 자격 증명"
    assert expected in result["detail"]
    assert calls == ["prod"]
    view = setup.view("demo")
    assert expected in next(s for s in view["checklist"] if s["id"] == "repository")["detail"]
    assert secret not in str(view) + setup._path("demo").joinpath("checks.json").read_text()


def test_machine_push_probe_failure_requires_settings_and_can_be_retried(panel, monkeypatch):
    _, service, _ = panel
    service.save_project_settings(
        "demo",
        {"repo_url": REPO, "git_author_name": "Operator", "git_author_email": "op@example.test"},
        updated_by="fixture",
        expected_version=0,
    )
    allowed = False

    def check(branch):
        if not allowed:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "설정 필요: 앱 저장소 push 권한")

    monkeypatch.setattr(
        service, "connect_repository", lambda ctx: SimpleNamespace(check_push_access=check)
    )
    setup = app._setup_service(
        service,
        Settings(adapter_mode=AdapterMode.REAL, llm_provider="replay", judgment_provider="replay"),
        "127.0.0.1",
    )
    assert setup.probe("demo", "repository")["status"] == "red"
    with pytest.raises(DdakToolError, match="연결 확인 필요"):
        setup.require_ready("demo")
    allowed = True
    setup.require_ready("demo")


@pytest.mark.parametrize("managed", [False, True])
def test_preparation_failure_persists_public_auth_and_profile(
    panel, monkeypatch, tmp_path, managed
):
    client, service, _ = panel
    service.onboarding = None
    service.save_project_settings(
        "demo",
        {
            "repo_url": REPO,
            "aws_profile": "team",
            "git_author_name": "Operator",
            "git_author_email": "op@example.test",
        },
        updated_by="fixture",
        expected_version=0,
    )
    secret = "fixture-run-only-push-value"
    if managed:
        save_token(service.root / "private", "demo", REPO, secret)

    def resolve(*args, **kwargs):
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "fixture resolve stop")

    monkeypatch.setattr(app, "resolve_head", resolve)
    run = asyncio.run(
        app._prepare_commit_inner(
            service,
            Settings(
                adapter_mode=AdapterMode.REAL, llm_provider="replay", judgment_provider="replay"
            ),
            WatchTarget("demo", REPO, "prod", "local"),
            None,
            policy=FetchPolicy(root=tmp_path / "sources"),
            trigger="manual",
        )
    )
    row = service.get_run(run)
    assert row["result"]["phase"] == "resolve"
    settings = redact_obj(row["context"]["project_settings"])
    assert settings["git_auth_source"] == ("managed" if managed else "machine")
    assert settings["aws_profile"] == "team"
    assert settings["aws_expected_account_id"] == "458781646776"
    assert secret not in str(row)
    from ddak.web.routes import results

    client.app.include_router(results.router)
    response = client.get(f"/runs/{run}/result")
    assert ("관리 페이지 토큰" if managed else "머신 Git 자격 증명") in response.text
    assert "team" in response.text and secret not in response.text


@pytest.mark.anyio
async def test_settings_and_auth_snapshot_survive_prepare_and_execution(rig):
    service, source, calls = rig
    service.save_project_settings(
        "demo", {"aws_profile": "team"}, updated_by="fixture", expected_version=0
    )
    selected = {
        "aws_profile": "team",
        "aws_expected_account_id": "458781646776",
        "git_auth_source": "machine",
    }
    p = plan()
    context = RunContext(p.run_id, project=p.project, toggles=p.toggles, project_settings=selected)
    service.prepare(p, context, source)
    assert all(
        service.approval_view(p.run_id)["project_settings"][k] == v for k, v in selected.items()
    )
    service.approve(p.run_id, approver="operator")
    service.start(p.run_id)
    await service.wait(p.run_id)
    for _, ctx in calls.contexts:
        assert all(ctx.project_settings[k] == v for k, v in selected.items())
    assert all(
        service.get_run(p.run_id)["context"]["project_settings"][k] == v
        for k, v in selected.items()
    )
