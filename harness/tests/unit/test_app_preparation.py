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


async def test_detected_commit_is_pinned_and_prepared(rig, monkeypatch):
    service, source, calls = rig
    service.save_project_settings(
        "demo",
        {"cloud_domain": "demo.example.test", "default_targets": "cloud"},
        updated_by="operator",
        expected_version=0,
    )
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
    assert seen[0][1]["platform"] == {"onprem": inventory}
    assert seen[0][1]["cloud_domain"] == "demo.example.test"
    run = service.get_run(rid)
    assert run["status"] == "AWAITING_APPROVAL"
    assert run["context"]["ref"] == "prod"
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
        def __init__(self, targets, handler, *, policy):
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
