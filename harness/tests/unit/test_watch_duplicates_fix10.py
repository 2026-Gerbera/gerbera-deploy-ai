"""source=fixture: 중복 자동 감시 저장/기동/운영 화면. 실제 Git/VM 접속 없음."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from ddak import app
from ddak.core.config import Settings
from ddak.core.project_settings import watch_source
from ddak.web.app import create_app
from ddak.web.routes.ops import router as ops_router
from tests.unit import test_deployment_service as support

rig = support.rig
REPO = "https://github.com/fixture/app"


def settings(repo=REPO, branch="prod", auto=True):
    return {
        "repo_url": repo,
        "watch_branch": branch,
        "auto_detect": auto,
        "default_targets": "onprem",
    }


def save(service, project, data, version=0):
    return service.save_project_settings(
        project, data, updated_by="fixture", expected_version=version
    )


def legacy_duplicates(service):
    # 이미 저장된 구버전 상태를 재현한다. 제품 저장 경로의 신규 중복 허용이 아니다.
    with service.store.connection() as db:
        for project in ("flaskr", "flaskr-three"):
            db.execute(
                "INSERT INTO project_settings VALUES (?, 1, ?, 'legacy-fixture', 0)",
                (project, json.dumps(settings())),
            )


@pytest.mark.parametrize(
    "repo,branch",
    [
        (REPO, "prod"),
        (REPO + ".git", "prod"),
        (REPO + "/", "refs/heads/prod"),
        ("https://GITHUB.com/Fixture/App.git", "prod"),
    ],
)
def test_saving_second_auto_watcher_transfers_owner_atomically(rig, repo, branch):
    service, _, _ = rig
    before = save(service, "flaskr", settings())
    saved = save(service, "flaskr-three", settings(repo, branch))
    assert saved["auto_detect"] is True and saved["version"] == 1
    previous = service.get_project_settings("flaskr")
    assert previous["auto_detect"] is False and previous["version"] == before["version"] + 1
    assert previous["repo_url"] == before["repo_url"]
    assert previous["watch_branch"] == before["watch_branch"]
    assert [t.project for t in app._watch_targets(service)] == ["flaskr-three"]


def test_unchanged_owner_and_different_branches_repos_manual_are_allowed(rig):
    service, _, _ = rig
    save(service, "flaskr", settings())
    assert save(service, "flaskr", {"default_targets": "both"}, 1)["version"] == 2
    save(service, "manual", settings(auto=False))
    save(service, "branch", settings(branch="release"))
    save(service, "repository", settings(repo=REPO + "-other"))
    assert save(service, "manual", {"auto_detect": True}, 1)["auto_detect"]
    assert service.get_project_settings("flaskr")["auto_detect"] is False
    assert service.get_project_settings("flaskr")["default_targets"] == "both"
    assert service.get_project_settings("branch")["auto_detect"] is True
    assert service.get_project_settings("repository")["auto_detect"] is True
    assert {t.project for t in app._watch_targets(service)} == {"manual", "branch", "repository"}


def test_simultaneous_auto_enable_cannot_create_two_owners(rig):
    service, _, _ = rig
    barrier = threading.Barrier(2)

    def attempt(name):
        barrier.wait(timeout=5)
        return save(service, name, settings())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("flaskr", "flaskr-three")))
    assert {row["project"] for row in results} == {"flaskr", "flaskr-three"}
    rows = service.list_project_settings()
    assert len(rows) == 2
    assert len([row for row in rows if row["auto_detect"]]) == 1
    assert sorted(row["version"] for row in rows) == [1, 2]
    assert len(app._watch_targets(service)) == 1


@pytest.mark.parametrize(
    "preferred,winner", [("flaskr-three", "flaskr-three"), (None, "flaskr"), ("other", "flaskr")]
)
def test_legacy_duplicate_targets_choose_one_and_show_both_names(
    rig, monkeypatch, preferred, winner
):
    service, _, _ = rig
    legacy_duplicates(service)
    monkeypatch.delenv("DDAK_WATCH_REPO_URL", raising=False)
    monkeypatch.delenv("DDAK_WATCH_PROJECT", raising=False)
    if preferred:
        monkeypatch.setenv("DDAK_WATCH_PROJECT", preferred)
    targets, warnings = app._watch_configuration(service)
    assert [t.project for t in targets] == [winner]
    assert len(warnings) == 1 and "flaskr-three" in warnings[0] and "flaskr" in warnings[0]
    assert all(s["auto_detect"] for s in service.list_project_settings())  # 기존 설정 기록 보존.
    assert app._watch_targets(service) == targets


def test_environment_fallback_cannot_duplicate_saved_repository(rig, monkeypatch):
    service, _, _ = rig
    save(service, "flaskr", settings())
    monkeypatch.setenv("DDAK_WATCH_PROJECT", "flaskr-three")
    monkeypatch.setenv("DDAK_WATCH_REPO_URL", REPO + ".git")
    monkeypatch.setenv("DDAK_WATCH_BRANCH", "prod")
    app._configure_onprem(service)  # 초기 프로필 복사로 중복 저장/기동 실패를 만들지 않는다.
    assert service.get_project_settings("flaskr-three") is None
    targets, warnings = app._watch_configuration(service)
    assert [t.project for t in targets] == ["flaskr-three"]
    assert warnings and "flaskr" in warnings[0]


def test_same_repo_different_branch_keeps_both_and_host_case_only_normalized():
    assert watch_source(REPO, "prod") != watch_source(REPO, "release")
    assert watch_source("https://git.example.com/Org/App") != watch_source(
        "https://git.example.com/org/app"
    )


def test_startup_dispatches_one_commit_and_warns_on_dashboard_and_ops(rig, monkeypatch):
    service, source, _ = rig
    legacy_duplicates(service)
    monkeypatch.setenv("DDAK_WATCH_PROJECT", "flaskr-three")
    monkeypatch.delenv("DDAK_WATCH_REPO_URL", raising=False)
    commits = []

    async def prepare(service, config, target, sha, **kwargs):
        commits.append(target.project)
        p = support.plan("run-one-watch").model_copy(update={"project": target.project})
        ctx = app.RunContext(p.run_id, project=target.project)
        return service.prepare(p, ctx, source)

    class FakeWatcher:
        def __init__(self, targets, handler, **kwargs):
            self.targets, self.handler = targets, handler
            self.stopped = asyncio.Event()

        async def run(self):
            for target in self.targets:
                await self.handler(target, "a" * 40)
            await self.stopped.wait()

        async def stop(self):
            self.stopped.set()

    monkeypatch.setattr(app, "Watcher", FakeWatcher)
    monkeypatch.setattr(app, "_prepare_commit", prepare)
    application = create_app(deployment_factory=lambda: service)
    application.include_router(ops_router)
    app._attach_watch(application, Settings())
    with TestClient(application, base_url="http://127.0.0.1:8765") as client:

        async def wait_for_run():
            for _ in range(100):
                if service.list_runs():
                    return
                await asyncio.sleep(0.01)
            pytest.fail("fixture watch did not dispatch")

        assert client.portal is not None
        client.portal.call(wait_for_run)
        for path in ("/", "/ops", "/settings"):
            response = client.get(path)
            assert response.status_code == 200
            assert "자동 감시 경고" in response.text
            assert "flaskr-three만 감시하고 flaskr는 제외" in response.text
        assert commits == ["flaskr-three"]
        assert len(service.list_runs()) == 1
        assert service.list_runs()[0]["project"] == "flaskr-three"


def test_bad_legacy_url_warns_without_disabling_valid_watch(rig, monkeypatch):
    service, _, _ = rig
    legacy_duplicates(service)
    with service.store.connection() as db:
        db.execute(
            "UPDATE project_settings SET data=? WHERE project='flaskr'",
            (json.dumps(settings(repo="https://github.com:bad/fixture/app")),),
        )
    monkeypatch.delenv("DDAK_WATCH_REPO_URL", raising=False)
    targets, warnings = app._watch_configuration(service)
    assert [t.project for t in targets] == ["flaskr-three"]
    assert warnings == ["자동 감시 설정 오류: flaskr의 저장소 URL을 확인하세요"]
    save(service, "different", settings(repo=REPO + "-other"))
    with pytest.raises(ValueError):
        save(service, "bad", settings(repo="https://github.com:bad/fixture/app"))


def test_saved_disabled_project_is_not_reenabled_by_environment(rig, monkeypatch):
    service, _, _ = rig
    save(service, "flaskr-three", settings(auto=False))
    monkeypatch.setenv("DDAK_WATCH_PROJECT", "flaskr-three")
    monkeypatch.setenv("DDAK_WATCH_REPO_URL", REPO)
    app._configure_onprem(service)
    assert app._watch_configuration(service) == ([], [])
