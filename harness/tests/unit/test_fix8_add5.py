"""추가5: 인프라 준비 격리와 HTTP 수명 밖 계획 요청(source=fixture)."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI, Request

from ddak import app
from ddak.core.config import AdapterMode, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.generate_infra import GenerateInfraInput, GenerateInfraOutput
from ddak.core.registry import Registry, spec_for
from ddak.executor.engine import RunStatus, TrackStatus
from ddak.plan.intake import FetchPolicy, WatchTarget
from ddak.web.routes.ops import operate
from tests.unit import test_deployment_service as support

rig = support.rig
pytestmark = pytest.mark.anyio


async def test_generate_infra_config_error_keeps_local_approval_and_execution(rig, monkeypatch):
    service, source, calls = rig
    registry = Registry(
        [*service.registry.specs, spec_for("generate_infra"), spec_for("apply_infra")]
    )
    for name in service.registry.registered():
        registry.tool(name)(service.registry.get(name).fn)

    @registry.tool("generate_infra")
    def generate(inp: GenerateInfraInput, ctx: RunContext) -> GenerateInfraOutput:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "fixture generator configuration missing")

    @registry.tool("apply_infra")
    def apply(inp: support.Input, ctx: RunContext) -> support.Output:
        pytest.fail("failed preparation must not apply")

    service.registry = registry

    def plan(request, **kwargs):
        p = support.plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
        p.deploy.cloud.steps.insert(
            0,
            support.step(
                "deploy.infra.cloud",
                "apply_infra",
                target=Target.CLOUD,
            ),
        )
        return SimpleNamespace(
            plan=p,
            context=RunContext(p.run_id, project=p.project, mode=p.mode),
            source=source,
        )

    original = app._infra_approval

    async def infra(service, plan, ctx):
        # 실제 generate tool 호출은 사용하되 Fake binding 자동 주입만 피한다.
        return await original(service, plan, replace(ctx, adapter_mode=AdapterMode.REAL))

    monkeypatch.setattr(app, "plan_deployment", plan)
    monkeypatch.setattr(app, "_infra_approval", infra)
    monkeypatch.setattr(app, "has_infra_binding", lambda _: False)
    rid = await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", "https://github.com/fixture/app", "prod", "both"),
        "a" * 40,
        policy=FetchPolicy(root=source.parent),
    )
    assert service.get_run(rid)["status"] == "AWAITING_APPROVAL"
    approval = service.approval_view(rid)
    assert approval["preparation_errors"]["cloud"]["code"] == "CONFIG_INVALID"
    assert "infra" not in approval["subjects"]
    service.approve(rid, approver="fixture")
    service.start(rid)
    result = await service.wait(rid)
    assert result.status is RunStatus.FAILED_CLOUD
    assert result.tracks["local"] is TrackStatus.DONE
    assert result.tracks["cloud"] is TrackStatus.FAILED
    assert any(r.step_id == "prepare.infra.cloud" for r in result.records)
    assert all("cloud" not in name and "rollback" not in name for name, _ in calls.contexts)


async def test_local_request_without_cloud_outputs_has_no_cloud_platform(rig, monkeypatch):
    service, source, _ = rig
    seen = []

    def plan(request, **kwargs):
        seen.append(kwargs["platform"])
        p = support.plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
        return SimpleNamespace(
            plan=p,
            context=RunContext(p.run_id, project=p.project, mode=p.mode),
            source=source,
        )

    monkeypatch.setattr(app, "plan_deployment", plan)
    rid = await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", "https://github.com/fixture/app", "prod", "local"),
        "a" * 40,
        policy=FetchPolicy(root=source.parent),
    )
    assert service.get_run(rid)["status"] == "AWAITING_APPROVAL"
    assert "cloud" not in seen[0]


async def test_http_plan_returns_before_flow_and_reuses_preparing_or_awaiting(rig):
    service, source, _ = rig
    service.save_project_settings(
        "demo",
        {"repo_url": "https://github.com/fixture/app"},
        updated_by="fixture",
        expected_version=0,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def flow(service, request):
        nonlocal count
        count += 1
        entered.set()
        await release.wait()
        p = support.plan("run-background")
        return service.prepare(
            p, RunContext(p.run_id, project=p.project, targets="onprem", ref="prod"), source
        )

    service.planning_flow = flow
    application = FastAPI()
    application.state.deployment = service
    application.state.settings = Settings()
    token = "a" * 40

    async def receive():
        return {
            "type": "http.request",
            "body": urlencode(
                {
                    "project": "demo",
                    "csrf_token": token,
                }
            ).encode(),
        }

    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/ops/plan",
            "app": application,
            "headers": [
                (b"host", b"127.0.0.1:8765"),
                (b"origin", b"http://127.0.0.1:8765"),
                (b"cookie", ("ddak_csrf=" + token).encode()),
                (b"content-type", b"application/x-www-form-urlencoded"),
            ],
        },
        receive,
    )
    try:
        response = await asyncio.wait_for(operate(req, "plan"), 0.2)
        assert response.status_code == 202
        data = json.loads(response.body)
        assert data["status"] == "PREPARING"
        await asyncio.wait_for(entered.wait(), 1)
        again = service.enqueue_deployment("demo")
        assert again["request_id"] == data["request_id"] and count == 1
        with pytest.raises(DdakToolError, match="다른 ref/대상"):
            service.enqueue_deployment("demo", ref="v2", targets="cloud")
        release.set()
        await service._preparation_tasks[data["request_id"]]
        ready = service.get_preparation(data["request_id"])
        assert ready["status"] == "AWAITING_APPROVAL"
        assert service.enqueue_deployment("demo")["run_id"] == ready["run_id"]
        with pytest.raises(DdakToolError, match="다른 ref/대상"):
            service.enqueue_deployment("demo", ref="v3", targets="cloud")
        assert count == 1
    finally:
        release.set()
        await service.shutdown()


async def test_auto_preparation_is_reused_and_shutdown_cancels_queued_work(rig):
    service, _, _ = rig
    await service.begin_preparation("demo", "auto-planning")
    with pytest.raises(DdakToolError, match="자동 계획 준비 중"):
        service.enqueue_deployment("demo")
    next_auto = asyncio.create_task(service.begin_preparation("demo", "next-auto-sha"))
    await asyncio.sleep(0)
    assert not next_auto.done()
    service.end_preparation("demo", "auto-planning")
    await asyncio.wait_for(next_auto, 1)
    assert service._preparing_runs["demo"] == "next-auto-sha"
    service.end_preparation("demo", "next-auto-sha")
    service.save_project_settings(
        "demo",
        {"repo_url": "https://github.com/fixture/app"},
        updated_by="fixture",
        expected_version=0,
    )
    entered = asyncio.Event()

    async def flow(*args):
        entered.set()
        await asyncio.Event().wait()

    service.planning_flow = flow
    data = service.enqueue_deployment("demo")
    await asyncio.wait_for(entered.wait(), 1)
    await service.shutdown()
    assert service.get_preparation(data["request_id"])["status"] == "CANCELLED"
