"""관문 사칭, 컨텍스트 경쟁, 취소와 실제 워커 수명을 검증한다."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from typing import Any

import pytest

from ddak.core.contracts.base import ContractModel
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Effect, Layer, Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.events import EventType, RunEvent
from ddak.core.contracts.plan import Plan, PlanStep
from ddak.core.registry import Registry
from ddak.executor.engine import Executor, RunResult, RunStatus, TrackStatus
from ddak.executor.events import EventBus
from tests.unit.test_executor import REG, Rollbacks, TInput, TOutput, _spec, plan, step

pytestmark = pytest.mark.anyio


def local_plan(tool: str = "worker", *, changing: bool = True) -> Plan:
    return Plan.model_validate(
        {
            "run_id": "run-1",
            "deploy": {
                "local": {
                    "steps": [
                        step(
                            "deploy.app.local",
                            tool=tool,
                            effect=Effect.STATE_CHANGE if changing else Effect.READ,
                        )
                    ],
                    "signal": "local_verified",
                },
                "cloud": {"steps": [step("verify.cloud", wait_for=["local_verified"])]},
            },
            "verify": {"steps": [step("verify.report", run="finally")]},
        }
    )


def worker_registry(**metadata: Any) -> Registry:
    registry = Registry([_spec("worker").model_copy(update=metadata), _spec("t_step")])

    @registry.tool("t_step")
    async def read(inp: TInput, ctx: RunContext) -> TOutput:
        return TOutput(passed=True)

    return registry


@pytest.mark.parametrize(
    "data",
    [
        {"build": {"steps": [step("build.a"), step("build.a")]}},
        {"build": {"steps": [step("build.a", signal="images_ready")], "signal": "images_ready"}},
        {"build": {"steps": [step("build.a", wait_for=["infra_ready"])]}},
        {
            "build": {
                "steps": [step("build.a", wait_for=["infra_ready"])],
                "signal": "images_ready",
            },
            "deploy": {
                "cloud": {
                    "steps": [step("cloud.a", wait_for=["images_ready"], signal="infra_ready")]
                }
            },
        },
        {
            "build": {"steps": [step("build.a", wait_for=["images_ready"])]},
            "verify": {"steps": [step("verify.a", signal="images_ready")]},
        },
        {"deploy": {"cloud": {"steps": [step("cloud.a", signal="local_verified")]}}},
    ],
)
async def test_invalid_graph_rejected_before_any_step(data: dict[str, Any]) -> None:
    called: list[str] = []

    async def before(current: PlanStep, ctx: RunContext) -> None:
        called.append(current.id)

    with pytest.raises(DdakToolError) as info:
        await Executor(REG, before_step=before).run(
            Plan(run_id="run-1", **data), RunContext("run-1")
        )
    assert info.value.code is ErrorCode.PLAN_INVALID
    assert called == []


@pytest.mark.parametrize("metadata", [{"effect": Effect.STATE_CHANGE}, {"requires_lock": True}])
async def test_registry_metadata_cannot_be_hidden_by_read_step(metadata: dict[str, Any]) -> None:
    registry = worker_registry(**metadata)
    p = Plan.model_validate(
        {"run_id": "run-1", "deploy": {"cloud": {"steps": [step("cloud.write", tool="worker")]}}}
    )
    with pytest.raises(DdakToolError, match="W1"):
        await Executor(registry).run(p, RunContext("run-1"))


@pytest.mark.parametrize(
    "metadata", [{"canonical": False}, {"layer": Layer.BUILTIN}, {"layer": Layer.OUTSIDE}]
)
async def test_outside_or_noncanonical_tool_rejected(metadata: dict[str, Any]) -> None:
    with pytest.raises(DdakToolError, match="계획에서 실행"):
        await Executor(worker_registry(**metadata)).run(local_plan(), RunContext("run-1"))


async def test_before_hook_and_missing_lock_fail_without_touch_or_rollback() -> None:
    called: list[str] = []
    registry = worker_registry(requires_lock=True)

    @registry.tool("worker")
    async def worker(inp: TInput, ctx: RunContext) -> TOutput:
        called.append("worker")
        return TOutput(passed=True)

    async def deny(current: PlanStep, ctx: RunContext) -> None:
        if current.tool == "worker":
            called.append("guard")
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "계획 해시 변경")

    rollback = Rollbacks()
    executor = Executor(registry, before_step=deny, rollback=rollback)
    missing = await executor.run(local_plan(), RunContext("run-1"))
    assert (missing.records[0].error or "").startswith("LOCK_INVALID")
    assert called == []
    denied = await executor.run(local_plan(), RunContext("run-1", lock_token="test-lock"))
    assert denied.status is RunStatus.FAILED_LOCAL
    assert called == ["guard"]
    assert rollback.targets == []
    assert denied.gates["local_verified"] is False


async def test_after_hooks_merge_freshest_context_and_before_sees_new_images() -> None:
    concurrent = peak = 0
    before_contexts: dict[str, RunContext] = {}

    async def before(current: PlanStep, ctx: RunContext) -> None:
        before_contexts[current.id] = ctx

    async def after(current: PlanStep, output: dict[str, Any], ctx: RunContext) -> RunContext:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.005)
        concurrent -= 1
        assert output["passed"] is True
        return replace(ctx, images={**ctx.images, current.id: "test-image"})

    p = Plan.model_validate(
        {
            "run_id": "run-1",
            "build": {"steps": [step("build.image")], "signal": "images_ready"},
            "deploy": {
                "local": {"steps": [step("local.read", wait_for=["images_ready"])]},
                "cloud": {"steps": [step("cloud.read")]},
            },
        }
    )
    result = await Executor(REG, before_step=before, after_step=after).run(p, RunContext("run-1"))
    assert result.status is RunStatus.SUCCEEDED
    assert result.context is not None
    assert set(result.context.images) == {"build.image", "local.read", "cloud.read"}
    assert "build.image" in before_contexts["local.read"].images
    assert result.context.deadline is None
    assert peak == 1
    assert RunResult("old", RunStatus.SUCCEEDED, {}, []).context is None


async def test_observation_hook_failure_closes_gate_before_cloud_starts() -> None:
    async def after(current: PlanStep, output: dict[str, Any], ctx: RunContext) -> RunContext:
        if current.signal == "local_verified":
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "이미지 관측 없음")
        return ctx

    rollback = Rollbacks()
    result = await Executor(REG, after_step=after, rollback=rollback).run(
        plan(), RunContext("run-1")
    )
    assert result.gates["local_verified"] is False
    assert result.tracks["cloud"] is TrackStatus.ABORTED_AT_GATE
    assert rollback.targets == [Target.LOCAL]


class MissingPassed(ContractModel):
    ok: bool = True


@pytest.mark.parametrize(
    "name", ["health_check", "smoke_test", "verify_tls", "compare_env_results"]
)
async def test_canonical_checks_require_explicit_passed_true(name: str) -> None:
    registry = Registry([_spec(name)])

    @registry.tool(name)
    async def missing(inp: TInput, ctx: RunContext) -> MissingPassed:
        return MissingPassed()

    p = Plan.model_validate(
        {
            "run_id": "run-1",
            "deploy": {
                "local": {"steps": [step("verify.check", tool=name, signal="local_verified")]}
            },
        }
    )
    result = await Executor(registry).run(p, RunContext("run-1"))
    assert result.status is RunStatus.FAILED_LOCAL
    assert result.gates["local_verified"] is False
    assert result.records[0].status == "check_failed"


async def test_sync_timeout_returns_while_worker_alive_without_racing_rollback() -> None:
    registry = worker_registry(effect=Effect.STATE_CHANGE)
    release, stopped = threading.Event(), threading.Event()
    observed: list[RunContext] = []

    @registry.tool("worker")
    def blocking(inp: TInput, ctx: RunContext) -> TOutput:
        observed.append(ctx)
        try:
            release.wait(3)
        finally:
            stopped.set()
        return TOutput(passed=True)

    rollback = Rollbacks()
    deadline = time.monotonic() + 0.05
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            Executor(registry, rollback=rollback).run(
                local_plan(), RunContext("run-1", deadline=deadline)
            ),
            1,
        )
        assert time.monotonic() - started < 0.8
        assert observed[0].deadline == deadline
        assert not stopped.is_set()
        assert result.status is RunStatus.NEEDS_HUMAN
        assert result.tracks["local"] is TrackStatus.ROLLBACK_FAILED
        assert result.gates["local_verified"] is False
        assert rollback.targets == []
        assert any(r.step_id == "verify.report" and r.status == "succeeded" for r in result.records)
    finally:
        release.set()
        await asyncio.to_thread(stopped.wait, 1)


async def test_tool_gets_timeout_deadline_when_run_has_no_deadline() -> None:
    registry = worker_registry(timeout_s=1)
    seen: list[float] = []

    @registry.tool("worker")
    def inspect_deadline(inp: TInput, ctx: RunContext) -> TOutput:
        assert ctx.deadline is not None
        seen.append(ctx.deadline - time.monotonic())
        return TOutput(passed=True)

    result = await Executor(registry).run(local_plan(), RunContext("run-1"))
    assert result.status is RunStatus.SUCCEEDED
    assert 0 < seen[0] <= 1


async def test_cancel_waits_for_async_cleanup_then_rolls_back_and_reports() -> None:
    registry = worker_registry()
    started, stopped = asyncio.Event(), asyncio.Event()
    order: list[str] = []

    @registry.tool("worker")
    async def blocking(inp: TInput, ctx: RunContext) -> TOutput:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.02)
            stopped.set()
            order.append("stopped")
        return TOutput(passed=True)

    async def rollback(target: Target, ctx: RunContext) -> None:
        assert stopped.is_set()
        order.append("rollback")

    task = asyncio.create_task(
        Executor(registry, rollback=rollback).run(local_plan(), RunContext("run-1"))
    )
    await started.wait()
    task.cancel()
    result = await asyncio.wait_for(task, 1)
    assert order == ["stopped", "rollback"]
    assert result.status is RunStatus.CANCELLED
    assert result.tracks["local"] is TrackStatus.ROLLED_BACK
    assert any(r.step_id == "verify.report" for r in result.records)


@pytest.mark.parametrize("sync", [False, True])
async def test_cancel_unquiesced_worker_is_needs_human(sync: bool) -> None:
    registry = worker_registry()
    entered = threading.Event()
    stop_thread = threading.Event()
    stop_async = asyncio.Event()

    if sync:

        @registry.tool("worker")
        def worker_sync(inp: TInput, ctx: RunContext) -> TOutput:
            entered.set()
            stop_thread.wait(3)
            return TOutput(passed=True)
    else:

        @registry.tool("worker")
        async def worker_async(inp: TInput, ctx: RunContext) -> TOutput:
            entered.set()
            try:
                await stop_async.wait()
            except asyncio.CancelledError:
                await stop_async.wait()
            return TOutput(passed=True)

    rollback = Rollbacks()
    task = asyncio.create_task(
        Executor(registry, rollback=rollback).run(local_plan(), RunContext("run-1"))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        result = await asyncio.wait_for(task, 1)
        assert result.status is RunStatus.NEEDS_HUMAN
        assert rollback.targets == []
        assert result.tracks["local"] is TrackStatus.ROLLBACK_FAILED
        assert any(r.step_id == "verify.report" for r in result.records)
    finally:
        stop_thread.set()
        stop_async.set()
        await asyncio.sleep(0.02)


@pytest.mark.parametrize("metadata", [{"effect": Effect.STATE_CHANGE}, {"requires_lock": True}, {}])
async def test_provider_timeout_is_unknown_only_for_mutating_or_locked_tool(
    metadata: dict[str, Any],
) -> None:
    registry = worker_registry(**metadata)

    @registry.tool("worker")
    def finished_cli(inp: TInput, ctx: RunContext) -> TOutput:
        raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "Docker 데몬 RPC 상태 불명")

    rollback = Rollbacks()
    changing = bool(metadata)
    result = await Executor(registry, rollback=rollback).run(
        local_plan(changing=False), RunContext("run-1", lock_token="test-lock")
    )
    assert result.status is (RunStatus.NEEDS_HUMAN if changing else RunStatus.FAILED_LOCAL)
    assert rollback.targets == []
    assert result.gates["local_verified"] is False


async def test_parity_rollback_failure_is_needs_human_and_keeps_local() -> None:
    result = await Executor(REG).run(plan(compare_mode="check_fail"), RunContext("run-1"))
    assert result.status is RunStatus.NEEDS_HUMAN
    assert result.tracks["local"] is TrackStatus.DONE
    assert result.tracks["cloud"] is TrackStatus.ROLLBACK_FAILED


async def test_report_self_cancel_does_not_change_actual_status() -> None:
    async def after(current: PlanStep, output: dict[str, Any], ctx: RunContext) -> RunContext:
        if current.run == "finally":
            raise asyncio.CancelledError
        return ctx

    result = await Executor(REG, after_step=after).run(plan(report_mode="ok"), RunContext("run-1"))
    assert result.status is RunStatus.SUCCEEDED
    assert result.records[-1].step_id == "verify.report"
    assert result.records[-1].status == "failed"


async def test_ui_errors_isolated_and_critical_events_first_in_sequence() -> None:
    bus = EventBus()
    persistent: list[RunEvent] = []
    ui: list[RunEvent] = []

    async def broken(event: RunEvent) -> None:
        raise RuntimeError("UI 끊김")

    async def collect(event: RunEvent) -> None:
        assert event in persistent
        ui.append(event)

    async def persist(event: RunEvent) -> None:
        await asyncio.sleep(0)
        persistent.append(event)

    bus.subscribe(broken)
    bus.subscribe(collect)
    bus.subscribe(persist, critical=True)
    result = await Executor(REG, bus=bus).run(plan(), RunContext("run-1"))
    assert result.status is RunStatus.SUCCEEDED
    assert [e.seq for e in persistent] == list(range(len(persistent)))
    assert all(e.run_id == "run-1" and e.ts for e in persistent)
    assert ui == persistent


async def test_critical_subscriber_propagates_and_ui_is_not_notified() -> None:
    bus = EventBus()
    ui: list[RunEvent] = []

    async def persist(event: RunEvent) -> None:
        raise OSError("저장 실패")

    async def collect(event: RunEvent) -> None:
        ui.append(event)

    bus.subscribe(collect)
    bus.subscribe(persist, critical=True)
    with pytest.raises(OSError):
        await bus.publish(RunEvent(run_id="run-1", seq=0, type=EventType.RUN_STATE))
    assert not ui


async def test_persistence_failure_does_not_suppress_rollback_or_final_report() -> None:
    bus = EventBus()
    rollback = Rollbacks()
    broken = False

    async def persist(event: RunEvent) -> None:
        nonlocal broken
        if event.type is EventType.STEP_FINISHED and event.step == "deploy.was.local":
            broken = True
        if broken:
            raise OSError("저장 실패")

    bus.subscribe(persist, critical=True)
    result = await asyncio.wait_for(
        Executor(REG, bus=bus, rollback=rollback).run(plan(report_mode="ok"), RunContext("run-1")),
        1,
    )
    assert rollback.targets == [Target.LOCAL]
    assert result.status is not RunStatus.SUCCEEDED
    assert any(r.step_id == "verify.report" for r in result.records)


async def test_initial_internal_failure_still_reports_and_fails_open_signals() -> None:
    bus = EventBus()

    async def persist(event: RunEvent) -> None:
        raise OSError("기록 불가")

    bus.subscribe(persist, critical=True)
    result = await Executor(REG, bus=bus).run(plan(report_mode="ok"), RunContext("run-1"))
    assert result.status is RunStatus.FAILED_BEFORE_DEPLOY
    assert all(value is False for value in result.gates.values())
    assert any(r.step_id == "verify.report" for r in result.records)


async def test_target_override_cannot_bypass_track_target() -> None:
    p = Plan.model_validate(
        {
            "run_id": "run-1",
            "deploy": {"local": {"steps": [step("deploy.bad", target=Target.CLOUD)]}},
        }
    )
    result = await Executor(REG).run(p, RunContext("run-1"))
    assert result.status is RunStatus.FAILED_LOCAL
    assert "target이 트랙과 다르다" in (result.records[0].error or "")


@pytest.mark.parametrize("ignore_cancel", [False, True])
async def test_hung_ui_is_removed_and_cannot_stall_deployment(ignore_cancel: bool) -> None:
    bus = EventBus()
    release = asyncio.Event()
    ended = asyncio.Event()
    calls = 0
    persisted: list[RunEvent] = []

    async def slow(event: RunEvent) -> None:
        nonlocal calls
        calls += 1
        try:
            try:
                await release.wait()
            except asyncio.CancelledError:
                if ignore_cancel:
                    await release.wait()
                else:
                    raise
        finally:
            ended.set()

    async def persist(event: RunEvent) -> None:
        persisted.append(event)

    bus.subscribe(slow)
    bus.subscribe(persist, critical=True)
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(Executor(REG, bus=bus).run(plan(), RunContext("run-1")), 1)
        assert result.status is RunStatus.SUCCEEDED
        assert time.monotonic() - started < 0.8
        assert calls == 1
        assert persisted[-1].status == "SUCCEEDED"
        assert [event.seq for event in persisted] == list(range(len(persisted)))
        if ignore_cancel:
            assert not ended.is_set()
    finally:
        release.set()
        await asyncio.wait_for(ended.wait(), 1)


async def test_provider_timeout_during_cancel_cleanup_stays_unknown() -> None:
    registry = worker_registry(effect=Effect.STATE_CHANGE)
    entered = asyncio.Event()

    @registry.tool("worker")
    async def remote_call(inp: TInput, ctx: RunContext) -> TOutput:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "RPC 결과 불명") from None
        return TOutput(passed=True)

    rollback = Rollbacks()
    task = asyncio.create_task(
        Executor(registry, rollback=rollback).run(local_plan(), RunContext("run-1"))
    )
    await entered.wait()
    task.cancel()
    result = await asyncio.wait_for(task, 1)
    assert result.status is RunStatus.NEEDS_HUMAN
    assert rollback.targets == []


async def test_quiesced_read_timeout_can_rollback_previous_mutation() -> None:
    registry = worker_registry()

    @registry.tool("worker")
    async def read_timeout(inp: TInput, ctx: RunContext) -> TOutput:
        raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "조회 시간 초과")

    p = local_plan(changing=False)
    p.deploy.local.steps.insert(0, step("deploy.prior", effect=Effect.STATE_CHANGE))
    rollback = Rollbacks()
    result = await Executor(registry, rollback=rollback).run(p, RunContext("run-1"))
    assert result.status is RunStatus.FAILED_LOCAL
    assert result.tracks["local"] is TrackStatus.ROLLED_BACK
    assert rollback.targets == [Target.LOCAL]


async def test_cancelled_rollback_is_failed_but_final_report_still_runs() -> None:
    async def rollback(target: Target, ctx: RunContext) -> None:
        raise asyncio.CancelledError

    result = await Executor(REG, rollback=rollback).run(
        plan(local_mode="raise", report_mode="ok"), RunContext("run-1")
    )
    assert result.status is RunStatus.NEEDS_HUMAN
    assert result.tracks["local"] is TrackStatus.ROLLBACK_FAILED
    assert any(r.step_id == "verify.report" and r.status == "succeeded" for r in result.records)


async def test_final_persistent_event_failure_preserves_tracks_and_requires_human() -> None:
    bus = EventBus()

    async def persist(event: RunEvent) -> None:
        if event.type is EventType.RUN_STATE and event.status == "SUCCEEDED":
            raise OSError("최종 기록 실패")

    bus.subscribe(persist, critical=True)
    result = await Executor(REG, bus=bus).run(plan(report_mode="ok"), RunContext("run-1"))
    assert result.status is RunStatus.NEEDS_HUMAN
    assert set(result.tracks.values()) == {TrackStatus.DONE}
    assert result.context is not None
    assert any(r.step_id == "verify.report" and r.status == "succeeded" for r in result.records)
