"""source=fixture: 최초 설정 RED. 실제 서비스/SQLite, 외부 실행 없음."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI

from ddak import app
from ddak.core.config import AdapterMode, Settings
from ddak.core.contracts.enums import LLMBackend
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.git_credentials import configured_identity
from ddak.core.registry import Registry
from ddak.core.store import Store
from ddak.executor.service import DeploymentService

REPO = "https://github.com/fixture/first-run"


def git_config_reader(monkeypatch, *, repo_path=None, local=None, global_values=None):
    """Git 설정 읽기 명령만 허용하는 응답 fixture. 실제 Git은 실행하지 않는다."""
    calls = []

    def read(argv, **kwargs):
        command = list(map(str, argv))
        assert command.pop(0) == "git" and not kwargs.get("shell")
        cwd = kwargs.get("cwd")
        if command[:1] == ["-C"]:
            cwd = command[1]
            command = command[2:]
        assert command[0] == "config"
        assert command[-2] == "--get" and command[-1] in {"user.name", "user.email"}
        assert set(command[1:-2]) <= {"--local", "--global"}
        scope = "global" if "--global" in command else "local"
        if scope == "local" and repo_path is not None:
            assert cwd is not None and Path(cwd).resolve() == repo_path.resolve()
        calls.append((scope, command[-1]))
        values = (global_values if scope == "global" else local) or {}
        value = values.get(command[-1])
        return subprocess.CompletedProcess(argv, 0 if value is not None else 1, value or "", "")

    monkeypatch.setattr(subprocess, "run", read)
    return calls


@pytest.fixture
def first_run(monkeypatch, tmp_path):
    # 실행 호스트의 설정/원격 표식이 fixture 판정을 바꾸지 않게 한다.
    remote_keys = {
        "SSH_CONNECTION",
        "SSH_CLIENT",
        "SSH_TTY",
        "container",
        "KUBERNETES_SERVICE_HOST",
        "ECS_CONTAINER_METADATA_URI",
        "ECS_CONTAINER_METADATA_URI_V4",
        "AWS_EXECUTION_ENV",
        "INVOCATION_ID",
        "CI",
        "WSL_DISTRO_NAME",
        "WSL_INTEROP",
    }
    for key in tuple(os.environ):
        if key.startswith("DDAK_") or key in remote_keys:
            monkeypatch.delenv(key)
    exists = Path.exists

    def local_exists(path):
        if str(path) in {"/.dockerenv", "/run/.containerenv"}:
            return False
        return exists(path)

    def forbidden_process(*args, **kwargs):
        pytest.fail("외부 프로세스 실행 금지: first-run fixture")

    monkeypatch.setattr(Path, "exists", local_exists)
    git_config_reader(monkeypatch)
    monkeypatch.setattr(subprocess, "Popen", forbidden_process)
    service = DeploymentService(Registry([]), tmp_path.resolve() / "state")
    # Settings()의 기존 FAKE/REPLAY 계약은 변경하지 않는다.
    base = Settings(llm_provider="replay", judgment_provider="replay")
    setup = app._setup_service(service, base, "127.0.0.1")
    service.onboarding = setup
    try:
        yield service, setup
    finally:
        service.close()


def test_legacy_duplicate_choices_save_transfers_normalized_watcher(first_run):
    service, setup = first_run
    # 구버전 DB에 이미 존재하는 두 owner를 재현한다. 서비스 저장을 mock하지 않는다.
    with service.store.connection() as db:
        for project, repo, branch in (
            ("old-app", REPO, "prod"),
            ("new-app", "https://GITHUB.com/Fixture/First-Run.git/", "refs/heads/prod"),
        ):
            data = {
                "repo_url": repo,
                "watch_branch": branch,
                "auto_detect": True,
                "default_targets": "onprem",
                "generation_provider": "replay",
                "judgment_provider": "replay",
            }
            db.execute(
                "INSERT INTO project_settings "
                "(project, version, data, updated_by, updated_at) VALUES (?, 1, ?, ?, 0)",
                (project, json.dumps(data), "legacy-fixture"),
            )
    old_before = service.get_project_settings("old-app")

    saved = setup.save_choices(
        "new-app", {"image_repository": "2026gerbera/flaskr"}, expected_version=1
    )

    assert saved["version"] == 2
    assert saved["image_repository"] == "2026gerbera/flaskr"
    assert saved["auto_detect"] is True
    old_after = service.get_project_settings("old-app")
    assert old_after["auto_detect"] is False
    assert old_after["version"] > old_before["version"]
    for field in ("repo_url", "watch_branch", "default_targets", "generation_provider"):
        assert old_after[field] == old_before[field]
    assert [s["project"] for s in service.list_project_settings() if s["auto_detect"]] == [
        "new-app"
    ]


def test_setup_and_project_settings_disjoint_stale_save_merges(first_run):
    service, setup = first_run
    project_form = {
        "repo_url": REPO,
        "watch_branch": "prod",
        "auto_detect": False,
        "code_patch": True,
        "default_targets": "onprem",
        "cloud_domain": None,
        "dns_mode": "external",
        "hosted_zone_id": None,
    }
    initial = service.save_project_settings(
        "demo", project_form, updated_by="fixture", expected_version=0
    )
    setup.save_choices(
        "demo",
        {"image_repository": "2026gerbera/flaskr"},
        expected_version=initial["version"],
    )

    # /settings가 보내는 실제 필드 묶음과 오래된 버전. DeploymentService의
    # 전체 dict 검증/Store 전달까지 통과시켜 setup 변경 손실도 검사한다.
    saved = service.save_project_settings(
        "demo",
        {**project_form, "watch_branch": "release"},
        updated_by="local-operator",
        expected_version=initial["version"],
    )

    assert saved["version"] == 3
    assert saved["watch_branch"] == "release"
    assert saved["image_repository"] == "2026gerbera/flaskr"
    assert saved["repo_url"] == REPO
    assert saved["auto_detect"] is False
    assert service.get_project_settings("demo") == saved


def test_invocation_id_wsl_loopback_allows_cli_choices(first_run, monkeypatch):
    service, setup = first_run
    monkeypatch.setenv("INVOCATION_ID", "fixture-systemd-invocation")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-fixture")
    monkeypatch.setenv("WSL_INTEROP", "/run/WSL/fixture_interop")

    saved = setup.save_choices(
        "demo",
        {
            "generation_provider": "claude-cli",
            "generation_model": "claude-sonnet-5-5",
            "judgment_provider": "claude-cli",
            "judgment_model": "claude-sonnet-5-5",
            "llm_effort": "low",
            "ai_timeout_s": 90,
        },
        expected_version=0,
    )

    assert saved["version"] == 1
    effective = setup.effective("demo", saved, setup.vault)
    assert effective.selected_provider("generation") == "claude-cli"
    assert effective.selected_provider("judgment") == "claude-cli"
    assert effective.ai_timeout_s == 90
    assert service.get_project_settings("demo")["generation_model"] == "claude-sonnet-5-5"


def test_real_defaults_preserve_fake_replay_fixture_contract(first_run, monkeypatch, tmp_path):
    # 실제 앱 설정 로더의 REAL 경로만 제품 TOML 기본값을 받는다.
    monkeypatch.chdir(tmp_path)
    for fixture in (Settings(), Settings.from_env({})):
        assert fixture.adapter_mode is AdapterMode.FAKE
        assert fixture.llm_backend is LLMBackend.REPLAY
        assert fixture.selected_provider("generation") == "replay"
    real = Settings.from_env({"DDAK_ADAPTER_MODE": "real"})
    assert real.selected_provider("generation") == "claude-cli"
    assert real.selected_provider("judgment") == "claude-cli"
    assert real.llm_model == real.judgment_model == "claude-sonnet-5-5"
    assert real.llm_effort == "low" and real.ai_timeout_s == 90
    assert real.build_backend == "local"
    assert real.image_repository == "2026gerbera/flaskr"
    _, manager = first_run
    assert manager.view("demo")["settings"]["watch_branch"] == "prod"


def test_empty_real_settings_are_ready_with_machine_identity_without_saving(first_run, monkeypatch):
    service, _ = first_run
    git_config_reader(
        monkeypatch,
        global_values={"user.name": "First Run Human", "user.email": "human@example.test"},
    )
    manager = app._setup_service(
        service, Settings.from_env({"DDAK_ADAPTER_MODE": "real"}), "127.0.0.1"
    )
    assert service.get_project_settings("demo") is None
    view = manager.view("demo")
    selected = view["settings"]
    assert selected["generation_provider"] == selected["judgment_provider"] == "claude-cli"
    assert selected["generation_model"] == selected["judgment_model"] == "claude-sonnet-5-5"
    assert selected["llm_effort"] == "low" and selected["ai_timeout_s"] == 90
    assert selected["build_backend"] == "local"
    assert selected["image_repository"] == "2026gerbera/flaskr"
    assert selected["watch_branch"] == "prod"
    assert selected["buildx_builder"].startswith("ddak-product-")
    assert selected["git_author_name"] == "First Run Human"
    assert view["setting_sources"]["git_author_name"] == "머신 git 신원"
    manager.require_ready("demo")
    assert service.get_project_settings("demo") is None


def test_managed_choices_override_machine_environment(first_run, monkeypatch):
    _, manager = first_run
    environment = {
        "DDAK_ADAPTER_MODE": "real",
        "DDAK_LLM_PROVIDER": "groq",
        "DDAK_LLM_MODEL": "machine-generation",
        "DDAK_JUDGMENT_PROVIDER": "groq",
        "DDAK_JUDGMENT_MODEL": "machine-judgment",
        "DDAK_LLM_EFFORT": "medium",
        "DDAK_AI_TIMEOUT_S": "20",
        "DDAK_BUILD_BACKEND": "codebuild",
        "DDAK_IMAGE_REPOSITORY": "fixture/machine",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    saved = {
        "generation_provider": "claude-cli",
        "generation_model": "claude-sonnet-5-5",
        "judgment_provider": "claude-cli",
        "judgment_model": "claude-sonnet-5-5",
        "llm_effort": "low",
        "ai_timeout_s": 90,
        "build_backend": "local",
        "image_repository": "2026gerbera/flaskr",
    }
    result = app._effective_project_settings(
        Settings.from_env(environment), "demo", saved, manager.vault, cli_host="127.0.0.1"
    )
    assert (
        result.selected_provider("generation")
        == result.selected_provider("judgment")
        == "claude-cli"
    )
    assert result.llm_model == result.judgment_model == "claude-sonnet-5-5"
    assert result.llm_effort == "low" and result.ai_timeout_s == 90
    assert result.build_backend == "local" and result.image_repository == "2026gerbera/flaskr"


def test_old_owner_stale_form_cannot_silently_reenable_auto_detect(first_run):
    service, _ = first_run
    original = {"repo_url": REPO, "watch_branch": "prod", "auto_detect": True}
    old = service.save_project_settings(
        "old-app", original, updated_by="fixture", expected_version=0
    )
    service.save_project_settings("new-app", original, updated_by="fixture", expected_version=0)
    new_before = service.get_project_settings("new-app")
    saved = service.save_project_settings(
        "old-app",
        {**original, "image_repository": "fixture/edited-after-handoff"},
        updated_by="fixture",
        expected_version=old["version"],
    )
    assert saved["auto_detect"] is False
    assert saved["image_repository"] == "fixture/edited-after-handoff"
    assert service.get_project_settings("new-app") == new_before
    assert [t.project for t in app._watch_targets(service)] == ["new-app"]


def test_same_field_stale_conflict_is_atomic_but_equal_value_is_allowed(first_run):
    service, manager = first_run
    original = manager.save_choices("demo", {"ai_timeout_s": 90}, expected_version=0)
    winner = manager.save_choices(
        "demo", {"ai_timeout_s": 60}, expected_version=original["version"]
    )
    with pytest.raises(DdakToolError) as conflict:
        manager.save_choices(
            "demo",
            {"ai_timeout_s": 120, "image_repository": "fixture/do-not-save"},
            expected_version=original["version"],
        )
    assert conflict.value.code is ErrorCode.PRECONDITION_FAILED
    assert service.get_project_settings("demo") == winner
    saved = manager.save_choices(
        "demo",
        {"ai_timeout_s": 60, "image_repository": "fixture/save-me"},
        expected_version=original["version"],
    )
    assert saved["ai_timeout_s"] == 60 and saved["image_repository"] == "fixture/save-me"


def test_handoff_target_write_failure_rolls_back_owner_and_allows_retry(first_run):
    service, _ = first_run
    data = {"repo_url": REPO, "watch_branch": "prod", "auto_detect": True}
    old = service.save_project_settings("old-app", data, updated_by="fixture", expected_version=0)
    with service.store.connection() as db:
        db.execute(
            "CREATE TRIGGER fixture_handoff_failure BEFORE INSERT ON project_settings "
            "WHEN NEW.project='new-app' BEGIN SELECT RAISE(ABORT, 'fixture-write-failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="fixture-write-failed"):
        service.save_project_settings("new-app", data, updated_by="fixture", expected_version=0)
    assert service.get_project_settings("old-app") == old
    assert service.get_project_settings("new-app") is None
    with service.store.connection() as db:
        db.execute("DROP TRIGGER fixture_handoff_failure")
    service.save_project_settings("new-app", data, updated_by="fixture", expected_version=0)
    assert service.get_project_settings("old-app")["auto_detect"] is False
    assert service.get_project_settings("new-app")["auto_detect"] is True


@pytest.mark.parametrize("origin", ["local", "global", "managed"])
def test_git_identity_reads_app_checkout_then_global_without_writes(first_run, monkeypatch, origin):
    service, _ = first_run
    checkout = service.root / "repositories" / "demo" / hashlib.sha256(REPO.encode()).hexdigest()
    (checkout / ".git").mkdir(parents=True)
    human = {"user.name": "Local Operator", "user.email": "local@example.test"}
    machine = {"user.name": "Global Operator", "user.email": "global@example.test"}
    calls = git_config_reader(
        monkeypatch,
        repo_path=checkout,
        local=human if origin == "local" else {},
        global_values=machine,
    )
    saved = (
        {"git_author_name": "Managed Operator", "git_author_email": "managed@example.test"}
        if origin == "managed"
        else {}
    )
    result = configured_identity(saved, repo_path=checkout)
    expected = {
        "local": ("Local Operator", "local@example.test"),
        "global": ("Global Operator", "global@example.test"),
        "managed": ("Managed Operator", "managed@example.test"),
    }
    assert result == expected[origin]
    if origin == "managed":
        assert calls == []
    else:
        assert ("local", "user.name") in calls and ("local", "user.email") in calls
        assert {scope for scope, _ in calls} == (
            {"local"} if origin == "local" else {"local", "global"}
        )
    service.save_project_settings(
        "demo", {"repo_url": REPO, **saved}, updated_by="fixture", expected_version=0
    )
    manager = app._setup_service(
        service, Settings.from_env({"DDAK_ADAPTER_MODE": "real"}), "127.0.0.1"
    )
    view = manager.view("demo")
    assert (view["settings"]["git_author_name"], view["settings"]["git_author_email"]) == expected[
        origin
    ]
    expected_source = "관리 페이지" if origin == "managed" else "머신 git 신원"
    assert view["setting_sources"]["git_author_name"] == expected_source
    assert view["setting_sources"]["git_author_email"] == expected_source
    assert list((checkout / ".git").iterdir()) == []


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"user.name": "Human With Missing Email"},
        {"user.name": "github-actions[bot]", "user.email": "actions@example.test"},
        {"user.name": "Claude", "user.email": "agent@example.test"},
        {"user.name": "Human Name", "user.email": "codex@example.test"},
    ],
)
def test_missing_or_bot_machine_identity_requires_settings(first_run, monkeypatch, identity):
    calls = git_config_reader(monkeypatch, global_values=identity)
    with pytest.raises(DdakToolError, match="설정 필요") as error:
        configured_identity({})
    assert error.value.code is ErrorCode.PRECONDITION_FAILED
    assert set(calls) == {("global", "user.name"), ("global", "user.email")}
    service, _ = first_run
    manager = app._setup_service(
        service, Settings.from_env({"DDAK_ADAPTER_MODE": "real"}), "127.0.0.1"
    )
    view = manager.view("demo")
    repository = next(item for item in view["checklist"] if item["id"] == "repository")
    assert repository["status"] == "red" and "설정 필요" in repository["detail"]
    assert view["setting_sources"]["git_author_name"] == "설정 필요"
    assert view["setting_sources"]["git_author_email"] == "설정 필요"
    with pytest.raises(DdakToolError, match="앱 저장소 push 권한"):
        manager.require_ready("demo")


async def exercise_handoff(first_run, monkeypatch, *, old_failure=None):
    service, _ = first_run
    source = {"repo_url": REPO, "watch_branch": "prod", "auto_detect": True}
    service.save_project_settings("old-app", source, updated_by="fixture", expected_version=0)
    entered, release, stopping, finished = (asyncio.Event() for _ in range(4))
    calls = []
    delivered = []
    errors = []

    async def prepare(service, config, target, sha, **kwargs):
        if not calls:
            entered.set()
            await release.wait()  # 이관이 저장되기 전 시작한 준비를 이관 중 완료한다.
        run_id = f"run-handoff-{len(calls) + 1}"
        calls.append((target.project, sha))
        if target.project == "old-app" and old_failure == "exception":
            raise RuntimeError("fixture old preparation failed")
        if target.project == "old-app" and old_failure == "status":
            service.store.preparation_failed(run_id, target.project, {"code": "INTERNAL"})
        else:
            service.store.create_run(run_id, target.project, "c" * 64)
        return run_id

    class FakeWatcher:
        def __init__(self, targets, handler, **kwargs):
            self.target, self.handler = targets[0], handler
            self.stopped = asyncio.Event()

        async def deliver(self, sha):
            delivered.append((self.target.project, sha))
            try:
                await self.handler(self.target, sha)
            except (DdakToolError, RuntimeError) as error:
                if self.target.project != "old-app" or old_failure is None:
                    raise
                errors.append(error)

        async def run(self):
            await self.deliver("a" * 40)
            if self.target.project == "old-app":
                await self.stopped.wait()
                await self.deliver("a" * 40)  # stop 직전 큐에 있던 이전 watcher 콜백.
            else:
                await self.deliver("b" * 40)  # 새 SHA는 정상 처리해야 한다.
                finished.set()
                await self.stopped.wait()

        async def stop(self):
            stopping.set()
            self.stopped.set()

    monkeypatch.setattr(app, "Watcher", FakeWatcher)
    # 외부 계획 준비만 대체하고 실제 _prepare_commit/감시 조립 경계는 보존한다.
    monkeypatch.setattr(app, "_prepare_commit_inner", prepare)
    application = FastAPI()
    application.state.deployment = service
    app._attach_watch(application, Settings())
    async with application.router.lifespan_context(application):
        try:
            await asyncio.wait_for(entered.wait(), 3)
            service.save_project_settings(
                "new-app",
                {**source, "repo_url": REPO + ".git", "watch_branch": "refs/heads/prod"},
                updated_by="fixture",
                expected_version=0,
            )
            await asyncio.wait_for(stopping.wait(), 3)
        finally:
            release.set()
        await asyncio.wait_for(finished.wait(), 3)
    assert delivered.count(("old-app", "a" * 40)) == 2
    assert ("new-app", "a" * 40) in delivered and ("new-app", "b" * 40) in delivered
    return service, calls, errors


@pytest.mark.anyio
async def test_handoff_inflight_late_callback_and_new_initial_trigger_create_one_run_per_sha(
    first_run, monkeypatch
):
    service, calls, errors = await exercise_handoff(first_run, monkeypatch)
    assert errors == []
    assert [sha for _, sha in calls].count("a" * 40) == 1
    assert calls[-1] == ("new-app", "b" * 40)
    assert len(service.list_runs()) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["status", "exception"])
async def test_failed_old_inflight_preparation_does_not_consume_new_owners_sha(
    first_run, monkeypatch, failure
):
    service, calls, errors = await exercise_handoff(first_run, monkeypatch, old_failure=failure)
    assert len(errors) == 1
    if failure == "status":
        assert isinstance(errors[0], DdakToolError) and errors[0].code is ErrorCode.INTERNAL
    else:
        assert str(errors[0]) == "fixture old preparation failed"
    assert calls == [("old-app", "a" * 40), ("new-app", "a" * 40), ("new-app", "b" * 40)]
    ready = [row for row in service.list_runs() if row["status"] == "AWAITING_APPROVAL"]
    assert len(ready) == 2 and all(row["project"] == "new-app" for row in ready)


@pytest.mark.parametrize(
    "trigger,failed,expected",
    [("auto", False, True), ("auto", True, False), ("manual", False, False)],
)
def test_persisted_auto_run_dedupes_normalized_source_after_store_reopen(
    first_run, trigger, failed, expected
):
    service, _ = first_run
    sha = "d" * 40
    service.store.create_run("run-persisted", "old-app", "e" * 64)
    service.store.save_prepared(
        "run-persisted",
        {
            "context": {
                "project": "old-app",
                "trigger": trigger,
                "repo_url": REPO,
                "ref": "prod",
                "source_sha": sha,
                "project_settings": {"watch_branch": "prod"},
            }
        },
    )
    if failed:
        service.store.preparation_failed("run-persisted", "old-app", {"code": "INTERNAL"})
    reopened = Store(service.store.path)
    assert (
        reopened.has_auto_run("https://GITHUB.com/Fixture/First-Run.git/", "refs/heads/prod", sha)
        is expected
    )
    assert not reopened.has_auto_run(REPO, "release", sha)
    assert not reopened.has_auto_run(REPO, "prod", "f" * 40)
    assert not reopened.has_auto_run(REPO + "-other", "prod", sha)
