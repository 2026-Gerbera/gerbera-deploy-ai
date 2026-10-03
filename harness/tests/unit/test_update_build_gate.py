"""기존 빌드 인프라가 준비된 UPDATE의 승인 전 대기 조립과 실행 순서를 검사한다."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ddak.app import _platform_bootstrap_plan
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Effect, RunMode, Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.events import EventType, RunEvent
from ddak.core.contracts.plan import Plan
from ddak.core.registry import Registry
from ddak.executor.engine import Executor, RunStatus, TrackStatus, check_signals
from ddak.executor.events import EventBus
from tests.unit import test_executor as f

CLOUD_OUTPUTS = {
    "codebuild_project_name": "existing-build",
    "image_repository": "existing/app",
}


def _context(
    mode: RunMode = RunMode.UPDATE,
    backend: str = "codebuild",
    cloud: dict[str, Any] | None = None,
    repository: str | None = "existing/app",
) -> RunContext:
    return RunContext(
        run_id="run-update-gate",
        mode=mode,
        build_backend=backend,
        image_repository=repository,
        platform={"cloud": dict(CLOUD_OUTPUTS if cloud is None else cloud)},
    )


def _plan(mode: RunMode = RunMode.UPDATE) -> Plan:
    return Plan.model_validate(
        {
            "run_id": "run-update-gate",
            "mode": mode,
            "plan_hash": "sha256:" + "a" * 64,
            "build": {
                "steps": [
                    f.step("build.was", tool="build_image", wait_for=["infra_ready"]),
                    f.step("build.assets", tool="build_image", wait_for=["infra_ready"]),
                ],
                "signal": "images_ready",
            },
            "deploy": {
                "cloud": {
                    "steps": [
                        f.step(
                            "deploy.infra.cloud",
                            tool="apply_infra",
                            target=Target.CLOUD,
                            effect=Effect.STATE_CHANGE,
                            signal="infra_ready",
                        ),
                        f.step(
                            "deploy.tls.cloud",
                            tool="t_cloud_only",
                            target=Target.CLOUD,
                            wait_for=["infra_ready"],
                        ),
                        f.step(
                            "deploy.was.cloud",
                            target=Target.CLOUD,
                            effect=Effect.STATE_CHANGE,
                            wait_for=["images_ready"],
                        ),
                    ]
                }
            },
        }
    )


@pytest.mark.parametrize("backend", ["codebuild", "local"])
@pytest.mark.parametrize("summary", [{"layer": "app"}, {"layer": "platform"}, None])
def test_update_removes_only_build_infra_wait_before_approval(backend, summary):
    original = _plan()
    original.deploy.local.steps.append(
        f.step("verify.smoke.local", target=Target.LOCAL, signal="local_verified")
    )
    original.build.steps[1].wait_for.append("local_verified")
    snapshot = original.model_dump()
    result = _platform_bootstrap_plan(original, _context(backend=backend), summary)

    assert result.plan_hash is None
    assert result.build.steps[0].wait_for == []
    assert result.build.steps[1].wait_for == ["local_verified"]
    assert result.deploy == original.deploy
    assert result.verify == original.verify
    assert original.model_dump() == snapshot
    check_signals(result)


@pytest.mark.parametrize(
    ("backend", "cloud", "repository"),
    [
        ("codebuild", {"image_repository": "existing/app"}, "existing/app"),
        ("codebuild", {"codebuild_project_name": "existing-build"}, "existing/app"),
        ("codebuild", {}, "existing/app"),
        ("codebuild", {**CLOUD_OUTPUTS, "codebuild_project_name": ""}, "existing/app"),
        ("codebuild", {**CLOUD_OUTPUTS, "codebuild_project_name": " "}, "existing/app"),
        ("codebuild", {**CLOUD_OUTPUTS, "codebuild_project_name": 1}, "existing/app"),
        ("codebuild", {**CLOUD_OUTPUTS, "image_repository": None}, "existing/app"),
        ("codebuild", {**CLOUD_OUTPUTS, "image_repository": " "}, "existing/app"),
        ("local", CLOUD_OUTPUTS, None),
        ("local", {}, None),
    ],
)
def test_missing_build_readiness_preserves_plan_and_hash(backend, cloud, repository):
    original = _plan()
    snapshot = original.model_dump()
    result = _platform_bootstrap_plan(
        original, _context(backend=backend, cloud=cloud, repository=repository), {"layer": "app"}
    )
    assert result is original
    assert result.model_dump() == snapshot


def test_local_ready_does_not_require_codebuild_outputs():
    result = _platform_bootstrap_plan(
        _plan(), _context(backend="local", cloud={}), {"layer": "app"}
    )
    assert all("infra_ready" not in step.wait_for for step in result.build.steps)
    assert result.plan_hash is None
    check_signals(result)


def test_update_without_build_infra_wait_keeps_hash():
    original = _plan()
    for step in original.build.steps:
        step.wait_for.clear()
    result = _platform_bootstrap_plan(original, _context(), {"layer": "app"})
    assert result is original


@pytest.mark.parametrize("backend", ["codebuild", "local"])
def test_bootstrap_keeps_infra_first_even_with_existing_outputs(backend):
    original = _plan(RunMode.BOOTSTRAP)
    original.deploy.cloud.steps[:2] = list(reversed(original.deploy.cloud.steps[:2]))
    for step in original.build.steps:
        step.wait_for.clear()
    snapshot = original.model_dump()
    result = _platform_bootstrap_plan(
        original, _context(RunMode.BOOTSTRAP, backend), {"layer": "platform"}
    )
    assert result.deploy.cloud.steps[0].tool == "apply_infra"
    assert result.deploy.cloud.steps[0].signal == "infra_ready"
    assert all(step.wait_for == ["infra_ready"] for step in result.build.steps)
    assert result.plan_hash is None
    assert original.model_dump() == snapshot
    check_signals(result)


def _seq(events: list[RunEvent], event_type: EventType, step: str) -> int:
    return next(event.seq for event in events if event.type is event_type and event.step == step)


async def _execute(plan: Plan, ctx: RunContext, *, parallel: bool, fail_infra: bool = False):
    registry = Registry(
        [f._spec(name) for name in ("build_image", "apply_infra", "t_cloud_only", "deploy_tier")]
    )
    build_started = asyncio.Event()
    infra_finished = asyncio.Event()
    events: list[RunEvent] = []
    bus = EventBus()

    async def collect(event: RunEvent) -> None:
        events.append(event)
        if event.type is EventType.STEP_FINISHED and event.step == "deploy.infra.cloud":
            infra_finished.set()

    bus.subscribe(collect)

    @registry.tool("build_image")
    async def build(inp: f.TInput, ctx: RunContext) -> f.TOutput:
        build_started.set()
        await infra_finished.wait()
        return f.TOutput(passed=True)

    @registry.tool("apply_infra")
    async def infra(inp: f.TInput, ctx: RunContext) -> f.TOutput:
        if parallel:
            await build_started.wait()
        if fail_infra:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "인프라 적용 실패")
        return f.TOutput(passed=True)

    @registry.tool("deploy_tier")
    @registry.tool("t_cloud_only")
    async def deploy(inp: f.TInput, ctx: RunContext) -> f.TOutput:
        return f.TOutput(passed=True)

    result = await asyncio.wait_for(
        Executor(registry, bus=bus, rollback=f.Rollbacks()).run(plan, ctx), timeout=2
    )
    return result, events


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["codebuild", "local"])
@pytest.mark.parametrize("mode", [RunMode.UPDATE, RunMode.BOOTSTRAP])
async def test_executor_build_order_and_cloud_infra_barrier(backend, mode):
    ctx = _context(mode, backend)
    prepared = _platform_bootstrap_plan(
        _plan(mode), ctx, {"layer": "platform" if mode is RunMode.BOOTSTRAP else "app"}
    )
    result, events = await _execute(prepared, ctx, parallel=mode is RunMode.UPDATE)

    assert result.status is RunStatus.SUCCEEDED
    build_start = _seq(events, EventType.STEP_STARTED, "build.was")
    infra_end = _seq(events, EventType.STEP_FINISHED, "deploy.infra.cloud")
    if mode is RunMode.UPDATE:
        assert build_start < infra_end
    else:
        assert infra_end < build_start
    cloud_start = _seq(events, EventType.STEP_STARTED, "deploy.was.cloud")
    assert infra_end < _seq(events, EventType.STEP_STARTED, "deploy.tls.cloud") < cloud_start
    assert _seq(events, EventType.STEP_FINISHED, "build.assets") < cloud_start


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["codebuild", "local"])
async def test_infra_failure_finishes_started_update_build_without_cloud_deploy(backend):
    ctx = _context(backend=backend)
    prepared = _platform_bootstrap_plan(_plan(), ctx, {"layer": "app"})
    result, events = await _execute(prepared, ctx, parallel=True, fail_infra=True)

    assert result.status is RunStatus.FAILED_CLOUD
    assert result.tracks["build"] is TrackStatus.DONE
    assert result.tracks["cloud"] is TrackStatus.FAILED
    assert result.gates["infra_ready"] is False
    assert (
        _seq(events, EventType.STEP_STARTED, "build.was")
        < _seq(events, EventType.STEP_FINISHED, "deploy.infra.cloud")
        < _seq(events, EventType.STEP_FINISHED, "build.was")
    )
    built = [record for record in result.records if record.tool == "build_image"]
    assert len(built) == 2
    assert all(record.status == "succeeded" for record in built)
    started = {event.step for event in events if event.type is EventType.STEP_STARTED}
    assert "deploy.was.cloud" not in started
    assert "deploy.tls.cloud" not in started


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("backend", "cloud", "repository"),
    [
        ("codebuild", {"image_repository": "existing/app"}, "existing/app"),
        ("codebuild", {"codebuild_project_name": "existing-build"}, "existing/app"),
        ("local", CLOUD_OUTPUTS, None),
    ],
)
async def test_missing_output_keeps_executor_build_wait(backend, cloud, repository):
    ctx = _context(backend=backend, cloud=cloud, repository=repository)
    prepared = _platform_bootstrap_plan(_plan(), ctx, {"layer": "app"})
    result, events = await _execute(prepared, ctx, parallel=False)
    assert result.status is RunStatus.SUCCEEDED
    assert _seq(events, EventType.STEP_FINISHED, "deploy.infra.cloud") < _seq(
        events, EventType.STEP_STARTED, "build.was"
    )
