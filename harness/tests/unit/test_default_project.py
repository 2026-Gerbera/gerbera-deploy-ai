"""source=fixture: 주소·실행환경 지정이 없을 때 저장된 프로젝트 설정으로 기본 프로젝트를 고른다."""

import pytest

from ddak.web.routes import setup, setup_actions
from tests.unit.test_ui_integration_fix10 import client_for
from tests.unit.test_ui_integration_fix10 import rig as rig

HTML = {"accept": "text/html"}
REPO = "https://github.com/fixture/app.git"


def save(service, project, **data):
    version = (service.get_project_settings(project) or {}).get("version", 0)
    service.save_project_settings(
        project, {"repo_url": REPO, **data}, updated_by="operator", expected_version=version
    )


def shown(client, path="/"):
    response = client.get(path, headers=HTML)
    assert response.status_code == 200
    return response.text


def label(project):
    # 개편 상단 표시는 긴 이름을 줄이므로 title로 전체 이름을 함께 준다.
    return f'<span class="project-label" title="{project}">{project}</span>'


@pytest.fixture(autouse=True)
def no_env_project(monkeypatch):
    monkeypatch.delenv("DDAK_WATCH_PROJECT", raising=False)


def test_single_saved_project_is_default(rig):
    service, _, _ = rig
    save(service, "flaskr-three", auto_detect=False)
    with client_for(service) as client:
        for path in ("/", "/settings", "/ops"):
            text = shown(client, path)
            assert label("flaskr-three") in text
            assert label("flaskr") not in text


def test_most_recently_updated_project_is_default(rig):
    service, _, _ = rig
    save(service, "zeta-app", auto_detect=False)
    save(service, "alpha-app", auto_detect=False)
    with client_for(service) as client:
        assert label("alpha-app") in shown(client)
        save(service, "zeta-app", watch_branch="main")
        assert label("zeta-app") in shown(client)


def test_watch_transfer_tie_prefers_new_watch_owner(rig):
    service, _, _ = rig
    save(service, "zeta-app", auto_detect=True)
    save(service, "alpha-app", auto_detect=True)
    rows = {s["project"]: s for s in service.list_project_settings()}
    assert rows["zeta-app"]["auto_detect"] is False
    assert rows["zeta-app"]["updated_at"] == rows["alpha-app"]["updated_at"]
    with client_for(service) as client:
        assert label("alpha-app") in shown(client)


def test_query_then_env_override_saved_settings(rig, monkeypatch):
    service, _, _ = rig
    save(service, "alpha-app", auto_detect=False)
    with client_for(service) as client:
        monkeypatch.setenv("DDAK_WATCH_PROJECT", "env-app")
        assert label("env-app") in shown(client)
        assert label("query-app") in shown(client, "/?project=query-app")
        monkeypatch.delenv("DDAK_WATCH_PROJECT")
        assert label("query-app") in shown(client, "/settings?project=query-app")
        assert label("alpha-app") in shown(client, "/settings")


def test_invalid_names_still_rejected(rig, monkeypatch):
    service, _, _ = rig
    with client_for(service) as client:
        assert client.get("/?project=Bad", headers=HTML).status_code == 400
        monkeypatch.setenv("DDAK_WATCH_PROJECT", "../etc")
        assert client.get("/", headers=HTML).status_code == 400


def test_no_saved_settings_shows_create_guidance(rig):
    service, _, _ = rig
    client = client_for(service)
    client.app.include_router(setup.router)
    client.app.include_router(setup_actions.router)
    with client:
        for path in ("/", "/settings", "/ops", "/setup", "/setup/actions"):
            text = shown(client, path)
            assert "data-project-required" in text and "프로젝트를 먼저 만드세요" in text
            assert '<form method="get" action="/settings">' in text
            assert "project-label" not in text and "?project=" not in text
        api = client.get("/ops/state")
        assert api.status_code == 404 and "프로젝트를 먼저 만드세요" in api.json()["detail"]
        # 안내의 다음 화면은 이름만 정해 연다. 열기만 해서는 프로젝트가 생기지 않는다.
        assert label("new-app") in shown(client, "/settings?project=new-app")
        assert service.list_project_settings() == []
