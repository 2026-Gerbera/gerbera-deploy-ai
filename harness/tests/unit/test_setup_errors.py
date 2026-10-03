"""초기 설정 오류는 HTML에 원인을 표시하고 입력 비밀값과 외부 예외는 숨긴다."""

import os
from pathlib import Path

import pytest

from ddak import app
from ddak.core.config import Settings, require_local_cli
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.registry import Registry
from ddak.executor.service import DeploymentService
from tests.unit.test_setup_service_fix11 import Builder
from tests.unit.test_setup_web_fix11 import HASH, panel, post  # noqa: F401

CONFLICT = "다른 화면에서 설정이 바뀌었습니다. 새로고침 후 다시 저장하세요"


@pytest.mark.parametrize(
    "action,method,data",
    [
        ("choices", "save_choices", {"version": "7"}),
        ("key", "save_key", {"provider_id": "custom"}),
        ("key-delete", "delete_key", {"provider_id": "custom"}),
        ("test", "test_provider", {"provider_id": "custom"}),
        ("status", "check_provider_status", {"provider_id": "custom"}),
        ("inventory", "register_inventory", {"inventory": "{}"}),
        ("env", "save_env", {"key": "APP_ENV"}),
        ("build-apply", "apply_build", {"plan_hash": HASH}),
        ("migration", "save_migration_url", {}),
        ("docker", "login_docker", {"username": "fixture"}),
        ("probe", "probe", {"kind": "ai"}),
        ("git-token", "save_git_token", {}),
        ("git-token-delete", "delete_git_token", {}),
    ],
)
def test_every_call_returns_safe_setup_alert(panel, action, method, data):  # noqa: F811
    client, coordinator = panel
    secret = "fixture-" + "opaque-credential"
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "설정 대상 <invalid> 확인 필요 " + secret + " password=hidden-fixture-value",
        )

    setattr(coordinator, method, fail)
    response = post(client, action, {**data, "value": secret, "token": secret})
    assert response.status_code == 409 and "application/json" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers
    error = response.json()["error"]
    assert error["code"] == "CONFIG_INVALID"
    assert "설정 대상 <invalid> 확인 필요" in error["message"]
    assert "[REDACTED]" in error["message"]
    assert secret not in response.text and "hidden-fixture-value" not in response.text
    assert len(calls) == 1


def test_general_precondition_and_duplicate_watch_keep_own_cause(panel):  # noqa: F811
    client, coordinator = panel
    for code, detail in [
        (ErrorCode.PRECONDITION_FAILED, "연결 검사 필요: AI"),
        (ErrorCode.CONFIG_INVALID, "자동 감시 중복: flaskr-three 프로젝트가 이미 감시합니다"),
    ]:
        coordinator.errors["save_choices"] = DdakToolError(code, detail)
        response = post(client, "choices", {"version": "7"})
        assert response.status_code == 409
        assert detail in response.text and code.value in response.text
        assert CONFLICT not in response.text


def test_view_failure_keeps_original_flash_without_secondary_exception(panel):  # noqa: F811
    client, coordinator = panel
    detail = "등록되지 않은 AI provider"
    coordinator.errors["save_choices"] = DdakToolError(ErrorCode.CONFIG_INVALID, detail)
    coordinator.errors["view"] = RuntimeError("must-not-display")
    response = client.post(
        "/setup/choices",
        data={"project": "other", "version": "7", "csrf_token": client.cookies["ddak_csrf"]},
        headers={"Origin": "http://127.0.0.1:8765", "Accept": "text/html"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client.get(response.headers["location"], headers={"Accept": "text/html"})
    assert response.status_code == 502 and detail in response.text
    assert "must-not-display" not in response.text and "<form" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert sum(name == "view" for name, _, _ in coordinator.calls) == 1


@pytest.fixture
def real_panel(panel, tmp_path, monkeypatch):  # noqa: F811
    client, _ = panel
    # 실제 사용자 설정·컨테이너 표식 대신 고정한 로컬 환경으로 조립 경로를 검증한다.
    for name in tuple(os.environ):
        if name.startswith("DDAK_"):
            monkeypatch.delenv(name)
    exists = Path.exists
    monkeypatch.setattr(
        Path,
        "exists",
        lambda p: False if str(p) in {"/.dockerenv", "/run/.containerenv"} else exists(p),
    )
    monkeypatch.setattr(
        app, "require_local_cli", lambda cfg, host: require_local_cli(cfg, host=host, environ={})
    )
    monkeypatch.setattr(app, "BuildSetup", Builder)

    def forbidden(*args, **kwargs):
        pytest.fail("설정 저장은 실제 제공자 호출을 하지 않는다")

    monkeypatch.setattr(app, "test_provider_connection", forbidden)
    monkeypatch.setattr(app, "provider_status", forbidden)
    service = DeploymentService(Registry([]), tmp_path / "state")
    settings = Settings(llm_provider="claude-api", judgment_provider="groq")
    service.onboarding = app._setup_service(service, settings, "127.0.0.1")
    client.app.state.deployment = service
    try:
        yield client, service, settings
    finally:
        service.close()


def test_claude_judgment_and_commit_author_save_through_real_assembly(real_panel):
    client, service, _ = real_panel
    response = post(
        client,
        "choices",
        {
            "version": "0",
            "generation_provider": "claude-api",
            "judgment_provider": "claude-cli",
            "git_author_name": "Operator",
            "git_author_email": "operator@example.test",
        },
    )
    assert response.status_code == 303
    saved = service.get_project_settings("other")
    assert saved["judgment_provider"] == "claude-cli"
    assert saved["git_author_name"] == "Operator"
    assert saved["git_author_email"] == "operator@example.test"
    response = client.get(response.headers["location"])
    assert response.status_code == 200
    assert "operator@example.test" in response.text
    assert 'value="claude-cli"' in response.text


def test_stale_version_returns_latest_saved_values_without_overwrite(real_panel):
    client, service, _ = real_panel
    saved = service.save_project_settings(
        "other", {"git_author_name": "Latest"}, updated_by="operator", expected_version=0
    )
    response = post(client, "choices", {"version": "0", "git_author_name": "Stale"})
    assert response.status_code == 409
    assert CONFLICT in response.text and "PRECONDITION_FAILED" in response.text
    refreshed = client.get("/setup?project=other")
    assert 'value="Latest"' in refreshed.text and 'value="Stale"' not in refreshed.text
    assert f'name="version" value="{saved["version"]}"' in refreshed.text
    assert service.get_project_settings("other") == saved


def test_invalid_provider_reason_and_cli_boundary_remain_visible(real_panel):
    client, service, settings = real_panel
    response = post(client, "choices", {"version": "0", "judgment_provider": "unknown"})
    assert response.status_code == 409
    assert "CONFIG_INVALID" in response.text and "등록되지 않은 AI provider" in response.text
    assert service.get_project_settings("other") is None
    service.onboarding = app._setup_service(service, settings, "192.0.2.1")
    response = post(client, "choices", {"version": "0", "judgment_provider": "claude-cli"})
    assert response.status_code == 409
    assert "CONFIG_INVALID" in response.text and "loopback 관리 웹" in response.text
    assert service.get_project_settings("other") is None
