"""앱 조립 경로: 외부 호출 없이 감지 SHA → 승인 대기/실패 기록."""

from types import SimpleNamespace

import pytest

from ddak import app
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.plan.intake import FetchPolicy, WatchTarget
from tests.unit import test_deployment_service as support

rig = support.rig
pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("language", ["ko", "ja"])
async def test_detected_commit_is_pinned_and_prepared(rig, monkeypatch, language):
    service, source, calls = rig
    service.save_project_settings(
        "demo",
        {
            "cloud_domain": "demo.example.test",
            "default_targets": "cloud",
        },
        updated_by="operator",
        expected_version=0,
    )
    service.remember_answer_language("demo", language)
    inventory = {"public_url": "https://onprem.example.test"}
    monkeypatch.setenv("DDAK_ONPREM_INVENTORY", "/fixture/inventory.yaml")
    monkeypatch.setattr(app, "load_inventory", lambda path: inventory)
    sha = "a" * 40
    seen = []

    def plan(request, **kwargs):
        seen.append((request, kwargs))
        p = support.plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
        ctx = RunContext(p.run_id, project=p.project, mode=p.mode)
        return SimpleNamespace(plan=p, context=ctx, source=source)

    monkeypatch.setattr(app, "plan_deployment", plan)
    rid = await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", "https://github.com/org/app", "prod", "both"),
        sha,
        policy=FetchPolicy(root=source.parent),
    )
    assert seen[0][0].ref == sha
    assert seen[0][1]["source_context"].project_settings["ai_answer_language"] == language
    assert seen[0][1]["platform"] == {
        "onprem": inventory,
        "cloud": {"region": "ap-northeast-2"},
    }
    assert seen[0][1]["cloud_domain"] == "demo.example.test"
    run = service.get_run(rid)
    assert run["status"] == "AWAITING_APPROVAL"
    assert run["context"]["ref"] == "prod"
    assert run["context"]["project_settings"]["ai_answer_language"] == language
    assert run["context"]["source_sha"] == sha
    assert run["context"]["repo_url"] == "https://github.com/org/app"
    view = service.approval_view(rid)
    assert view["trigger"] == "auto"
    assert view["targets"] == "both"  # settings default must not silently narrow explicit target
    assert view["project_settings"]["version"] == 1
    assert calls.contexts == []  # no execution before human approval


@pytest.mark.parametrize("where", ["plan", "prepare"])
async def test_preparation_failure_is_visible_without_exception_contents(rig, monkeypatch, where):
    service, source, _ = rig

    def plan(request, **kwargs):
        if where == "plan":
            raise RuntimeError("sensitive exception content must not be recorded")
        p = support.plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
        return SimpleNamespace(
            plan=p,
            context=RunContext(p.run_id, project=p.project, mode=p.mode),
            source=source,
        )

    monkeypatch.setattr(app, "plan_deployment", plan)
    if where == "prepare":
        monkeypatch.setattr(
            service, "prepare", lambda *a: (_ for _ in ()).throw(ValueError("private"))
        )
    rid = await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", "https://github.com/org/app", "prod"),
        "b" * 40,
        policy=FetchPolicy(root=source.parent),
    )
    assert service.list_runs()[0]["run_id"] == rid
    result = service.get_run(rid)
    assert result["status"] == "FAILED_BEFORE_DEPLOY"
    assert result["result"]["phase"] == where
    assert "private" not in str(result) and "sensitive exception" not in str(result)


async def test_cloud_request_ignores_onprem_inventory(rig, monkeypatch):
    service, source, _ = rig
    monkeypatch.setenv("DDAK_ONPREM_INVENTORY", "/missing/inventory.yaml")

    def no_inventory(path):
        pytest.fail("cloud-only request must not read onprem inventory")

    def plan(request, **kwargs):
        p = support.plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
        return SimpleNamespace(
            plan=p,
            context=RunContext(p.run_id, project=p.project, mode=p.mode),
            source=source,
        )

    monkeypatch.setattr(app, "load_inventory", no_inventory)
    monkeypatch.setattr(app, "plan_deployment", plan)
    rid = await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", "https://github.com/org/app", "prod", "cloud"),
        "c" * 40,
        policy=FetchPolicy(root=source.parent),
    )
    assert service.get_run(rid)["status"] == "AWAITING_APPROVAL"
    assert service.approval_view(rid)["targets"] == "cloud"


async def test_failure_details_and_approved_context_are_preserved(rig):
    from ddak.core.contracts.errors import DdakToolError, ErrorCode

    service, source, _ = rig
    rid = support.prepare(service, source)
    service.approve(rid, approver="operator")
    before = service.get_run(rid)
    service.record_preparation_failure(
        rid,
        "demo",
        DdakToolError(ErrorCode.CONFIG_INVALID, "계획 설정 오류"),
        context=RunContext(rid, project="demo", source_sha="f" * 40),
    )
    assert service.get_run(rid) == before
    service.record_preparation_failure(
        "failed-plan",
        "demo",
        DdakToolError(ErrorCode.CONFIG_INVALID, "계획 설정 오류"),
        phase="plan",
    )
    assert service.get_run("failed-plan")["result"]["detail"] == "계획 설정 오류"
    assert service.get_run("failed-plan")["result"]["phase"] == "plan"


async def test_watcher_retries_recorded_failure(rig, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    service, source, _ = rig
    monkeypatch.setenv("DDAK_WATCH_REPO_URL", "https://github.com/org/app")
    monkeypatch.setenv("DDAK_SOURCES_DIR", str(source.parent / "intake"))
    calls = []

    def plan(request, **kwargs):
        calls.append(request.ref)
        if len(calls) == 1:
            raise RuntimeError("temporary failure")
        p = support.plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
        return SimpleNamespace(
            plan=p,
            context=RunContext(p.run_id, project=p.project, mode=p.mode),
            source=source,
        )

    done = asyncio.Event()
    real_watcher = app.Watcher

    class OneCommitWatcher:
        def __init__(self, targets, handler, *, policy, **kwargs):
            self.delegate = real_watcher(targets, handler, policy=policy, interval_s=0.001)
            self.target = targets[0]

        async def run(self):
            await self.delegate._handle(self.target, "e" * 40)
            done.set()

        async def stop(self):
            await self.delegate.stop()

    @asynccontextmanager
    async def lifespan(a):
        a.state.deployment = service
        yield

    monkeypatch.setattr(app, "plan_deployment", plan)
    monkeypatch.setattr(app, "Watcher", OneCommitWatcher)
    application = FastAPI(lifespan=lifespan)
    app._attach_watch(application, Settings())
    async with application.router.lifespan_context(application):
        await asyncio.wait_for(done.wait(), 2)
    assert calls == ["e" * 40, "e" * 40]
    assert {row["status"] for row in service.list_runs()} == {
        "AWAITING_APPROVAL",
        "FAILED_BEFORE_DEPLOY",
    }


async def test_settings_change_during_source_copy_rejects_preparation(rig, monkeypatch):
    service, source, _ = rig
    service.save_project_settings(
        "demo",
        {"cloud_domain": "old.example.test"},
        updated_by="operator",
        expected_version=0,
    )

    def plan(request, **kwargs):
        p = support.plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
        return SimpleNamespace(
            plan=p,
            context=RunContext(p.run_id, project=p.project, mode=p.mode),
            source=source,
        )

    copy = app.copy_source

    def change_then_copy(src, dest):
        service.save_project_settings(
            "demo",
            {"cloud_domain": "new.example.test"},
            updated_by="operator",
            expected_version=1,
        )
        return copy(src, dest)

    monkeypatch.setattr(app, "plan_deployment", plan)
    monkeypatch.setattr(app, "copy_source", change_then_copy)
    rid = await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", "https://github.com/org/app", "prod"),
        "c" * 40,
        policy=FetchPolicy(root=source.parent),
    )
    assert service.get_run(rid)["status"] == "FAILED_BEFORE_DEPLOY"
    assert service.get_run(rid)["result"]["code"] == "PRECONDITION_FAILED"
    assert service.get_run(rid)["context"]["project_settings"]["version"] == 1
    assert service.get_run(rid)["context"]["project_settings"]["cloud_domain"] == "old.example.test"
    assert not (service.root / "sources" / rid).exists()


async def test_watch_targets_follow_saved_settings_without_env(rig, monkeypatch):
    service, _source, _calls = rig
    monkeypatch.delenv("DDAK_WATCH_REPO_URL", raising=False)
    service.save_project_settings(
        "demo",
        {
            "repo_url": "https://github.com/team/app",
            "watch_branch": "prod",
            "auto_detect": True,
            "default_targets": "onprem",
        },
        updated_by="operator",
        expected_version=0,
    )
    assert app._watch_targets(service) == [
        app.WatchTarget("demo", "https://github.com/team/app", "prod", "local")
    ]
    service.save_project_settings(
        "demo", {"auto_detect": False}, updated_by="operator", expected_version=1
    )
    monkeypatch.setenv("DDAK_WATCH_REPO_URL", "https://github.com/wrong/repo")
    assert app._watch_targets(service) == []  # 저장된 OFF를 환경변수로 우회하지 않는다.


@pytest.mark.parametrize("missing", ["build_image", "smoke_test"])
async def test_missing_tool_keeps_plan_settings_and_removes_owned_source(rig, monkeypatch, missing):
    import json

    from ddak.core.contracts.errors import DdakToolError, ErrorCode
    from ddak.core.registry import UnknownToolError

    service, source, _ = rig
    settings = service.save_project_settings(
        "demo",
        {
            "repo_url": "https://github.com/org/app",
            "watch_branch": "dev",
            "default_targets": "onprem",
        },
        updated_by="operator",
        expected_version=0,
    )
    keep = service.root / "sources" / "unrelated"
    keep.mkdir(parents=True)
    (keep / "keep.txt").write_text("preserve")

    def plan(request, **kwargs):
        p = support.plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
        return SimpleNamespace(
            plan=p, context=RunContext(p.run_id, project=p.project, mode=p.mode), source=source
        )

    registered = service.registry.get

    def get(name):
        if name == missing:
            raise UnknownToolError(name)
        return registered(name)

    monkeypatch.setattr(service.registry, "get", get)
    monkeypatch.setattr(app, "plan_deployment", plan)
    rid = await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", settings["repo_url"], "dev", "local"),
        "b" * 40,
        policy=FetchPolicy(root=source.parent),
    )
    row = service.get_run(rid)
    assert row["status"] == "FAILED_BEFORE_DEPLOY"
    assert row["result"]["phase"] == "prepare"
    assert row["result"]["code"] == "CONFIG_INVALID"
    assert row["result"]["missing_tool"] == missing
    assert row["context"]["project_settings"]["version"] == 1
    assert row["context"]["project_settings"]["watch_branch"] == "dev"
    saved_plan = json.loads((service.root / "runs" / rid / "plan.json").read_text())
    assert any(
        s["tool"] == missing
        for section in (saved_plan["build"], saved_plan["deploy"]["local"])
        for s in section["steps"]
    )
    assert not (service.root / "sources" / rid).exists()
    assert (keep / "keep.txt").read_text() == "preserve"
    assert source.exists()
    with pytest.raises(DdakToolError) as error:
        service.approval_view(rid)
    assert error.value.code is ErrorCode.PRECONDITION_FAILED


async def test_failed_copy_keeps_preexisting_source_directory(rig, monkeypatch):
    service, source, _ = rig
    rid = "existing-copy-run"
    destination = service.root / "sources" / rid
    destination.mkdir(parents=True)
    (destination / "keep.txt").write_text("preserve")
    monkeypatch.setattr(app, "new_run_id", lambda: rid)

    def plan(request, **kwargs):
        p = support.plan(rid).model_copy(update={"mode": request.mode})
        return SimpleNamespace(
            plan=p, context=RunContext(rid, project="demo", mode=p.mode), source=source
        )

    monkeypatch.setattr(app, "plan_deployment", plan)
    await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", "https://github.com/org/app", "prod"),
        "c" * 40,
        policy=FetchPolicy(root=source.parent),
    )
    assert service.get_run(rid)["result"]["phase"] == "source"
    assert (destination / "keep.txt").read_text() == "preserve"


@pytest.mark.parametrize("target", ["local", "cloud"])
async def test_first_selected_environment_bootstraps_after_other_success(rig, monkeypatch, target):
    from ddak.core.contracts.enums import RunMode

    service, source, _ = rig
    previous = {"local": {}, "cloud": {}}
    previous[target] = None
    monkeypatch.setattr(app, "_previous_manifests", lambda *a: previous)
    seen = []

    def plan(request, **kwargs):
        seen.append(request.mode)
        p = support.plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
        return SimpleNamespace(
            plan=p, context=RunContext(p.run_id, project="demo", mode=p.mode), source=source
        )

    monkeypatch.setattr(app, "plan_deployment", plan)
    rid = await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", "https://github.com/org/app", "prod", target),
        "c" * 40,
        policy=FetchPolicy(root=source.parent),
    )
    assert seen == [RunMode.BOOTSTRAP]
    assert service.get_run(rid)["status"] == "AWAITING_APPROVAL"


async def test_mixed_initial_states_require_initial_target_only(rig, monkeypatch):
    service, source, _ = rig
    monkeypatch.setattr(app, "_previous_manifests", lambda *a: {"local": {}, "cloud": None})
    monkeypatch.setattr(
        app, "plan_deployment", lambda *a, **kw: pytest.fail("mixed mode must not plan")
    )
    rid = await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", "https://github.com/org/app", "prod", "both"),
        "c" * 40,
        policy=FetchPolicy(root=source.parent),
    )
    row = service.get_run(rid)
    assert row["status"] == "FAILED_BEFORE_DEPLOY"
    assert row["result"]["code"] == "PRECONDITION_FAILED"
    assert "대상만 선택" in row["result"]["detail"]


async def test_cancelled_copy_finishes_before_owned_source_cleanup(rig, monkeypatch):
    import asyncio
    import threading

    service, source, _ = rig
    copying, release = threading.Event(), threading.Event()
    real_copy = app.copy_source
    rid = "cancel-copy-run"
    monkeypatch.setattr(app, "new_run_id", lambda: rid)

    def plan(request, **kwargs):
        p = support.plan(rid).model_copy(update={"mode": request.mode})
        return SimpleNamespace(
            plan=p, context=RunContext(rid, project="demo", mode=p.mode), source=source
        )

    def copy(src, dest):
        copying.set()
        assert release.wait(5)
        return real_copy(src, dest)

    monkeypatch.setattr(app, "plan_deployment", plan)
    monkeypatch.setattr(app, "copy_source", copy)
    task = asyncio.create_task(
        app._prepare_commit(
            service,
            Settings(),
            WatchTarget("demo", "https://github.com/org/app", "prod"),
            "c" * 40,
            policy=FetchPolicy(root=source.parent),
        )
    )
    try:
        assert await asyncio.to_thread(copying.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.get_run(rid)["result"]["phase"] == "source"
    assert not (service.root / "sources" / rid).exists()
    assert source.exists()


@pytest.mark.parametrize("failed", [False, True])
async def test_facts_export_and_intake_ttl_do_not_remove_approval_source(rig, monkeypatch, failed):
    import json
    import os
    import time

    from ddak.core.contracts.errors import DdakToolError, ErrorCode
    from ddak.core.contracts.plan_facts import EnvKey, Facts
    from ddak.core.snapshots import preview
    from ddak.plan.intake import cleanup_stale_sources

    service, source, _calls = rig
    rid = "facts-intake-run"
    policy = FetchPolicy(root=source.parent / "intake")
    intake = policy.root / "runs" / "demo" / rid
    intake.mkdir(parents=True)
    (intake / "app.py").write_text("VERSION=1\n")
    snapshot = preview(intake)
    monkeypatch.setattr(app, "new_run_id", lambda: rid)

    def plan(request, **kwargs):
        p = support.plan(rid).model_copy(update={"mode": request.mode})
        return SimpleNamespace(
            plan=p,
            context=RunContext(rid, project="demo", mode=p.mode),
            source=intake,
            facts=Facts(
                project="demo",
                mode=p.mode,
                target=request.target,
                tiers=("was",),
                changed={"local": {"was": True}},
                env_keys=(
                    EnvKey(name="SESSION_KEY", kind="secret", reason="fixture-value-never-persist"),
                ),
                source_snapshot_hash=snapshot.source_snapshot_hash,
                facts_hash=snapshot.source_snapshot_hash,
            ),
        )

    monkeypatch.setattr(app, "plan_deployment", plan)
    if failed:

        def reject(*args, **kwargs):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "fixture failure")

        monkeypatch.setattr(service, "prepare", reject)
    await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", "https://github.com/org/app", "prod", "local"),
        "a" * 40,
        policy=policy,
    )
    facts_path = service.root / "runs" / rid / "facts.json"
    assert "fixture-value-never-persist" not in facts_path.read_text()
    assert json.loads(facts_path.read_text())["facts_hash"] == snapshot.source_snapshot_hash
    assert facts_path.stat().st_mode & 0o777 == 0o600
    old = time.time() - policy.cache_ttl_s - 10
    os.utime(intake, (old, old))
    assert f"runs/demo/{rid}" in cleanup_stale_sources(policy.root, policy.cache_ttl_s)
    assert not intake.exists()
    assert (service.root / "sources" / rid).exists() is not failed
    if not failed:
        assert service.approval_view(rid)["run_id"] == rid
