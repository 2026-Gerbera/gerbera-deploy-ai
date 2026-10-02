"""레지스트리 실행기: 병렬 트랙, 신호 관문, 환경별 롤백과 최종 보고.

승인·해시·잠금 소유권은 service의 before_step, 산출물 병합은 after_step으로 연결한다.
취소된 to_thread는 실제 스레드 종료가 아니다. 종료 불명 작업과 롤백을 겹치지 않는다.
변경 툴의 ADAPTER_TIMEOUT도 상태 불명이다. CLI가 종료되어도 Docker 데몬의 RPC는
계속될 수 있으므로 자동 롤백 없이 NEEDS_HUMAN을 반환하고 service가 잠금을 유지한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from ddak.core import runtime
from ddak.core.config import AdapterMode
from ddak.core.contracts.base import ToolInput
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Effect, Layer, Target, ToolKind
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.events import EventType, RunEvent
from ddak.core.contracts.plan import CLOUD_VERIFIED, LOCAL_VERIFIED, Plan, PlanStep, Section
from ddak.core.redact import redact
from ddak.core.registry import PING, RegisteredTool, Registry, ToolSpec, UnknownToolError
from ddak.executor.events import EventBus
from ddak.executor.selection import select_plan

TRACK_BUILD, TRACK_LOCAL, TRACK_CLOUD, TRACK_VERIFY = "build", "local", "cloud", "verify"
_RESERVED_PARAMS = frozenset({"run_id", "target", "tier", "lock_token"})

RollbackHook = Callable[[Target, RunContext], Awaitable[None]]
BeforeStepHook = Callable[[PlanStep, RunContext], Awaitable[None]]
AfterStepHook = Callable[[PlanStep, dict[str, Any], RunContext], Awaitable[RunContext]]
ToolContextHook = Callable[[PlanStep, Target | None, RunContext], RunContext]
_QUIESCE_TIMEOUT_S = 0.1
_ROLLBACK_TIMEOUT_S = 300.0
_VERIFIED_TOOLS = frozenset({"health_check", "smoke_test", "verify_tls", "compare_env_results"})


class TrackStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    DONE = "DONE"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    NOT_APPLICABLE = "N/A"
    SKIPPED = "SKIPPED"  # 빌드 의존성 실패 또는 교차 검증 조건 미충족


class RunStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED_BEFORE_DEPLOY = "FAILED_BEFORE_DEPLOY"  # 빌드 실패 우선. 선행 인프라는 별도 기록
    FAILED_LOCAL = "FAILED_LOCAL"  # 온프렘 실패/복구, 클라우드는 독립 진행
    FAILED_CLOUD = "FAILED_CLOUD"  # 클라우드 실패/복구, 온프렘은 새 버전 유지
    FAILED_VERIFY = "FAILED_VERIFY"  # 교차 비교 이외의 공통 검증 실패
    PARITY_FAILED = "PARITY_FAILED"  # 교차 검증 불일치. 클라우드만 롤백, 로컬 유지
    CANCELLED = "CANCELLED"  # 실행 종료와 필요한 롤백을 확인한 취소
    NEEDS_HUMAN = "NEEDS_HUMAN"  # 롤백 실패. 사람이 정리


class GateFailed(Exception):
    def __init__(self, gate: str, why: str) -> None:
        super().__init__(f"{gate}: {why}")
        self.gate = gate
        self.why = why


class StepFailed(Exception):
    def __init__(self, step_id: str, code: ErrorCode | None, message: str) -> None:
        super().__init__(f"{step_id}: {code.value if code else 'CHECK_FAILED'}: {message}")
        self.step_id = step_id
        self.code = code
        self.message = message


class Gate:
    """대기 지점 신호. 한 번 열리거나 실패하면 바뀌지 않는다."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._event = asyncio.Event()
        self.ok: bool | None = None
        self.why: str | None = None

    @property
    def settled(self) -> bool:
        return self._event.is_set()

    def open(self) -> None:
        if not self.settled:
            self.ok = True
            self._event.set()

    def fail(self, why: str) -> None:
        if not self.settled:
            self.ok, self.why = False, why
            self._event.set()

    async def wait(self) -> None:
        await self._event.wait()
        if not self.ok:
            raise GateFailed(self.name, self.why or "")


@dataclass
class StepRecord:
    step_id: str
    tool: str
    target: Target | None
    status: str  # succeeded | failed | check_failed
    elapsed_s: float
    output: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class RunResult:
    run_id: str
    status: RunStatus
    tracks: dict[str, TrackStatus]
    records: list[StepRecord]
    gates: dict[str, bool | None] = field(default_factory=dict)
    context: RunContext | None = None
    infra_changes: list[dict[str, Any]] = field(default_factory=list)


class _RunState:
    def __init__(self, ctx: RunContext, bus: EventBus | None) -> None:
        self.ctx = ctx
        self.bus = bus
        self.gates: dict[str, Gate] = {}
        self.tracks: dict[str, TrackStatus] = {}
        self.records: list[StepRecord] = []
        self.infra_changes: list[dict[str, Any]] = []
        self.context_lock = asyncio.Lock()
        self.event_lock = asyncio.Lock()
        self.touched: set[Target] = set()
        self.app_touched: set[Target] = set()
        self.unquiesced: set[Target | None] = set()
        self.work: dict[asyncio.Task[Any], Target | None] = {}
        self.stopping = False
        self.event_failed = False
        self._seq = 0

    def gate(self, name: str) -> Gate:
        return self.gates.setdefault(name, Gate(name))

    def fail_gates(self, why: str) -> None:
        for gate in self.gates.values():
            gate.fail(why)

    async def emit(self, type_: EventType, **fields: Any) -> None:
        async with self.event_lock:
            event = RunEvent(
                run_id=self.ctx.run_id,
                seq=self._seq,
                type=type_,
                ts=datetime.now(UTC).isoformat(timespec="milliseconds"),
                **fields,
            )
            self._seq += 1
            if self.bus is not None:
                try:
                    await self.bus.publish(event)
                except Exception:
                    self.event_failed = self.stopping = True
                    self.fail_gates("필수 이벤트 기록 실패")
                    raise


def _sections(plan: Plan) -> dict[str, Section]:
    return {
        TRACK_BUILD: plan.build,
        TRACK_LOCAL: plan.deploy.local,
        TRACK_CLOUD: plan.deploy.cloud,
        TRACK_VERIFY: plan.verify,
    }


def _changes_state(step: PlanStep, spec: ToolSpec) -> bool:
    return Effect.STATE_CHANGE in (step.effect, spec.effect) or spec.requires_lock


def _check_tool(step: PlanStep, spec: ToolSpec, ctx: RunContext) -> None:
    # 기존 fake ping 예제만 허용한다. service의 실제 계획 검사는 예외 없이 canonical을 요구한다.
    fake_ping = (
        ctx.adapter_mode is AdapterMode.FAKE
        and spec.name == PING
        and spec.read_only
        and spec.effect is Effect.READ
        and not spec.requires_lock
    )
    if (
        (not spec.canonical and not fake_ping)
        or spec.kind is not ToolKind.TOOL_FN
        or spec.layer in (Layer.BUILTIN, Layer.OUTSIDE)
        or step.layer in (Layer.BUILTIN, Layer.OUTSIDE)
    ):
        raise DdakToolError(ErrorCode.PLAN_INVALID, f"계획에서 실행할 수 없는 툴: {step.tool}")
    if step.run == "finally" and _changes_state(step, spec):
        raise DdakToolError(ErrorCode.PLAN_INVALID, "finally 보고는 상태를 바꿀 수 없다")


def check_signals(plan: Plan, registry: Registry | None = None) -> None:
    """실행 전 DAG 검사: 순차 step, 섹션 종료, verify 시작 장벽도 의존성이다."""
    sections = _sections(plan)
    producers: dict[str, str] = {}
    dependencies: dict[str, set[str]] = {}
    ids: set[str] = set()

    def produce(signal: str | None, node: str) -> None:
        if signal:
            if signal in producers:
                raise DdakToolError(ErrorCode.PLAN_INVALID, f"신호 생산자 중복: {signal}")
            producers[signal] = node

    for name, section in sections.items():
        previous: str | None = None
        for step in section.steps:
            if step.id in ids:
                raise DdakToolError(ErrorCode.PLAN_INVALID, f"step id 중복: {step.id}")
            ids.add(step.id)
            if len(set(step.wait_for)) != len(step.wait_for):
                raise DdakToolError(ErrorCode.PLAN_INVALID, f"대기 신호 중복: {step.id}")
            if step.run is not None:
                if name != TRACK_VERIFY:
                    raise DdakToolError(ErrorCode.PLAN_INVALID, "finally는 verify 섹션에만 둔다")
                if step.wait_for or step.signal:
                    raise DdakToolError(
                        ErrorCode.PLAN_INVALID, "finally는 신호를 기다리거나 열지 않는다"
                    )
                continue
            dependencies[step.id] = {previous} if previous else set()
            if name == TRACK_VERIFY:
                dependencies[step.id].update(f"@{track}" for track in sections if track != name)
            previous = step.id
            produce(step.signal, step.id)
        end = f"@{name}"
        dependencies[end] = {previous} if previous else set()
        if name == TRACK_VERIFY:
            dependencies[end].update(f"@{track}" for track in sections if track != name)
        produce(section.signal, end)

    for name, section in sections.items():
        for step in section.steps:
            for wanted in step.wait_for:
                if (name == TRACK_CLOUD and wanted == LOCAL_VERIFIED) or (
                    name == TRACK_LOCAL and wanted == CLOUD_VERIFIED
                ):
                    raise DdakToolError(
                        ErrorCode.PLAN_INVALID, "환경 간 검증 대기는 허용하지 않는다"
                    )
                if wanted not in producers:
                    raise DdakToolError(
                        ErrorCode.PLAN_INVALID, f"아무도 열지 않는 신호: {step.id} -> {wanted}"
                    )
                dependencies[step.id].add(producers[wanted])

    # Kahn 순회: bootstrap의 cloud -> build -> local -> cloud도 step 순서로 판정한다.
    remaining = {node: set(parents) for node, parents in dependencies.items()}
    while remaining:
        ready = {node for node, parents in remaining.items() if not parents}
        if not ready:
            raise DdakToolError(ErrorCode.PLAN_INVALID, "신호 의존성 순환(교착)")
        remaining = {
            node: parents - ready for node, parents in remaining.items() if node not in ready
        }

    # G1/G2를 다른 트랙이 직접 열면 검증 관문을 사칭할 수 있다.
    for signal, name in ((LOCAL_VERIFIED, TRACK_LOCAL), (CLOUD_VERIFIED, TRACK_CLOUD)):
        section = sections[name]
        allowed = {step.id for step in section.steps} | {f"@{name}"}
        if signal in producers and producers[signal] not in allowed:
            raise DdakToolError(ErrorCode.PLAN_INVALID, f"신호 생산 트랙이 다르다: {signal}")


def build_input(
    registered: RegisteredTool, step: PlanStep, track_target: Target | None, ctx: RunContext
) -> ToolInput:
    """step과 실행 컨텍스트로 입력 모델을 만든다. 스키마로 한 번 더 검증된다(extra=forbid)."""
    spec = registered.spec
    if step.target is not None and track_target is not None and step.target is not track_target:
        raise DdakToolError(ErrorCode.PLAN_INVALID, f"{step.id}: target이 트랙과 다르다")
    target = step.target or track_target
    if target is not None and spec.targets and target not in spec.targets:
        allowed = ", ".join(t.value for t in spec.targets)
        msg = f"{spec.name}은 {target.value} 대상이 없다(대상: {allowed})"
        raise DdakToolError(ErrorCode.PLAN_INVALID, msg)
    if _RESERVED_PARAMS & step.params.keys():
        raise DdakToolError(ErrorCode.PLAN_INVALID, f"{step.id}: params로 예약 키를 바꿀 수 없다")
    fields = registered.input_model.model_fields
    data: dict[str, Any] = {"run_id": ctx.run_id}
    if "target" in fields and target is not None:
        data["target"] = target
    if "tier" in fields and step.tier is not None:
        data["tier"] = step.tier
    if "lock_token" in fields and ctx.lock_token is not None:
        data["lock_token"] = ctx.lock_token
    data.update(step.params)
    return registered.input_model.model_validate(data)


def decide_status(tracks: dict[str, TrackStatus]) -> RunStatus:
    bad = {TrackStatus.FAILED, TrackStatus.ROLLED_BACK}
    local, cloud = tracks.get(TRACK_LOCAL), tracks.get(TRACK_CLOUD)
    if TrackStatus.ROLLBACK_FAILED in tracks.values():
        return RunStatus.NEEDS_HUMAN
    if tracks.get(TRACK_BUILD) in bad:
        return RunStatus.FAILED_BEFORE_DEPLOY
    if local in bad:
        return RunStatus.FAILED_LOCAL
    if cloud in bad:
        return RunStatus.FAILED_CLOUD
    if TrackStatus.SKIPPED in (local, cloud):
        return RunStatus.FAILED_BEFORE_DEPLOY
    verify = tracks.get(TRACK_VERIFY)
    if verify in bad:
        return RunStatus.FAILED_VERIFY
    return RunStatus.SUCCEEDED


class Executor:
    def __init__(
        self,
        registry: Registry,
        *,
        bus: EventBus | None = None,
        rollback: RollbackHook | None = None,
        before_step: BeforeStepHook | None = None,
        after_step: AfterStepHook | None = None,
        tool_context: ToolContextHook | None = None,
        on_invoke: Callable[[PlanStep, RunContext], None] | None = None,
        rollback_timeouts: Mapping[Target, float] | None = None,
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._rollback = rollback
        self._before_step = before_step
        self._after_step = after_step
        self._tool_context = tool_context
        self._on_invoke = on_invoke
        self._rollback_timeouts = dict(rollback_timeouts or {})

    async def run(self, plan: Plan, ctx: RunContext) -> RunResult:
        if plan.run_id != ctx.run_id:
            raise DdakToolError(ErrorCode.PLAN_INVALID, "plan.run_id와 실행 컨텍스트가 다르다")
        plan = select_plan(plan, ctx)
        check_signals(plan, self._registry)
        for name, section in _sections(plan).items():
            for step in section.steps:
                try:
                    spec = self._registry.spec(step.tool)
                except UnknownToolError:
                    continue
                _check_tool(step, spec, ctx)
                if _changes_state(step, spec) and name not in (TRACK_LOCAL, TRACK_CLOUD):
                    raise DdakToolError(ErrorCode.PLAN_INVALID, "상태 변경은 배포 트랙에만 둔다")
        state = _RunState(ctx, self._bus)
        for name, section in _sections(plan).items():
            state.tracks[name] = TrackStatus.PENDING
            for signal in [section.signal, *(step.signal for step in section.steps)]:
                if signal:
                    state.gate(signal)
        tasks: dict[str, asyncio.Task[None]] = {}
        cancelled = internal_failure = False
        try:
            await state.emit(EventType.RUN_STATE, status="DEPLOYING")
            for name, section, target in (
                (TRACK_BUILD, plan.build, None),
                (TRACK_LOCAL, plan.deploy.local, Target.LOCAL),
                (TRACK_CLOUD, plan.deploy.cloud, Target.CLOUD),
            ):
                tasks[name] = asyncio.create_task(self._track(name, section, target, state))
            done, _ = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()
            tasks[TRACK_VERIFY] = asyncio.create_task(self._verify(plan.verify, state))
            await asyncio.wait({tasks[TRACK_VERIFY]})
            tasks[TRACK_VERIFY].result()

        except asyncio.CancelledError:
            if state.event_failed:
                internal_failure = True
            else:
                cancelled = True
        except Exception:
            internal_failure = True
        finally:
            # 별도 task를 shield해 재차 취소되어도 제한된 정리와 최종 보고를 완료한다.
            finishing = asyncio.create_task(
                self._finish(plan, state, tasks, cancelled, internal_failure)
            )
            while True:
                try:
                    result = await asyncio.shield(finishing)
                    break
                except asyncio.CancelledError:
                    if finishing.cancelled():
                        raise
        return result

    async def _finish(
        self,
        plan: Plan,
        state: _RunState,
        tasks: dict[str, asyncio.Task[None]],
        cancelled: bool,
        internal_failure: bool,
    ) -> RunResult:
        if cancelled or internal_failure:
            state.stopping = True
            state.fail_gates("실행 취소 또는 내부 실패")
            pending = {task for task in tasks.values() if not task.done()}
            for task in pending:
                task.cancel()
            if pending:
                _, pending = await asyncio.wait(pending, timeout=2 * _QUIESCE_TIMEOUT_S)
            for name, task in tasks.items():
                if task in pending:
                    target = Target(name) if name in (TRACK_LOCAL, TRACK_CLOUD) else None
                    state.unquiesced.add(target)
                    task.add_done_callback(_consume_exception)
                elif not task.cancelled():
                    _consume_exception(task)
                if state.tracks[name] in (TrackStatus.RUNNING, TrackStatus.WAITING):
                    state.tracks[name] = TrackStatus.FAILED
            # 실행 wrapper가 취소되어도 underlying sync worker의 생존 여부를 따로 확인한다.
            state.unquiesced.update(
                target for task, target in state.work.items() if not task.done()
            )
            for target in state.app_touched:
                if state.tracks[target.value] not in (
                    TrackStatus.ROLLED_BACK,
                    TrackStatus.ROLLBACK_FAILED,
                ):
                    state.tracks[target.value] = await self._rollback_track(target, state)

        failures = [r for r in state.records if r.status in {"failed", "check_failed"}]
        verify_ids = {s.id for s in plan.verify.steps if s.run is None}
        parity_failed = any(
            r.step_id in verify_ids
            and r.status == "check_failed"
            and r.tool in {"compare_env_results", "diagnose_parity_gap"}
            for r in failures
        )
        for target in {
            r.target
            for r in failures
            if r.step_id in verify_ids
            and r.target
            and r.tool not in {"compare_env_results", "diagnose_parity_gap"}
        }:
            if state.tracks[target.value] is TrackStatus.DONE:
                state.tracks[target.value] = (
                    await self._rollback_track(target, state)
                    if target in state.app_touched
                    else TrackStatus.FAILED
                )
        if (
            not cancelled
            and not internal_failure
            and parity_failed
            and state.tracks[TRACK_CLOUD] is TrackStatus.DONE
        ):
            state.tracks[TRACK_CLOUD] = (
                await self._rollback_track(Target.CLOUD, state)
                if Target.CLOUD in state.app_touched
                else TrackStatus.FAILED
            )
        for target in state.unquiesced:
            if target is not None:
                state.tracks[target.value] = TrackStatus.ROLLBACK_FAILED
        status = decide_status(state.tracks)
        if state.unquiesced or status is RunStatus.NEEDS_HUMAN:
            status = RunStatus.NEEDS_HUMAN
        elif cancelled:
            status = RunStatus.CANCELLED
        elif state.tracks[TRACK_BUILD] is TrackStatus.FAILED:
            status = RunStatus.FAILED_BEFORE_DEPLOY
        elif parity_failed:
            status = RunStatus.PARITY_FAILED
        elif internal_failure and status is RunStatus.SUCCEEDED:
            status = RunStatus.FAILED_BEFORE_DEPLOY

        for step in plan.verify.steps:
            if step.run == "finally":
                # 보고의 예외·타임아웃·자체 취소는 배포 판정을 바꾸지 않는다.
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await self._call(step, None, state, final=True)
        try:
            await state.emit(EventType.RUN_STATE, status=status.value)
        except (Exception, asyncio.CancelledError):
            if state.touched:
                # 배포 결과 자체는 tracks에 보존한다. 필수 최종 기록 실패는 잠금을 유지한다.
                status = RunStatus.NEEDS_HUMAN
        return RunResult(
            run_id=state.ctx.run_id,
            status=status,
            tracks=dict(state.tracks),
            records=list(state.records),
            gates={name: gate.ok for name, gate in state.gates.items()},
            context=state.ctx,
            infra_changes=list(state.infra_changes),
        )

    async def _skip(self, step: PlanStep, state: _RunState) -> None:
        state.records.append(StepRecord(step.id, step.tool, step.target, "skipped", 0))
        await state.emit(
            EventType.STEP_SKIPPED,
            step=step.id,
            tool=step.tool,
            target=step.target,
            status="skipped",
            detail="검증 대상 트랙의 성공 조건 미충족",
        )

    async def _verify(self, section: Section, state: _RunState) -> None:
        failed_targets: set[Target] = set()
        failed = ran = skipped = False
        for step in section.steps:
            if step.run == "finally":
                continue
            both_ok = not failed_targets and all(
                state.tracks.get(t.value) is TrackStatus.DONE for t in Target
            )
            eligible = (
                both_ok
                if step.target is None
                or step.tool in {"compare_env_results", "diagnose_parity_gap"}
                else (
                    step.target not in failed_targets
                    and state.tracks.get(step.target.value) is TrackStatus.DONE
                )
            )
            if step.tool == "diagnose_parity_gap" and any(
                r.tool == "compare_env_results" and r.status == "failed" for r in state.records
            ):
                eligible = False
            if not eligible:
                skipped = True
                await self._skip(step, state)
                continue
            ran = True
            await self._track(
                TRACK_VERIFY,
                section.model_copy(update={"steps": [step], "signal": None}),
                None,
                state,
            )
            status = state.tracks[TRACK_VERIFY]
            if status is TrackStatus.SKIPPED:
                skipped = True
                await self._skip(step, state)
            elif status is not TrackStatus.DONE:
                failed = True
                if step.target:
                    failed_targets.add(step.target)
        state.tracks[TRACK_VERIFY] = (
            TrackStatus.FAILED if failed else TrackStatus.DONE if ran else TrackStatus.SKIPPED
        )
        if section.signal:
            if failed or skipped or not ran:
                state.gate(section.signal).fail("검증 성공 조건 미충족")
            else:
                await state.emit(EventType.GATE_OPENED, detail=section.signal)
                state.gate(section.signal).open()

    async def _track(
        self, name: str, section: Section, target: Target | None, state: _RunState
    ) -> None:
        if target is not None and not section.steps:
            state.tracks[name] = TrackStatus.NOT_APPLICABLE
            if section.signal:
                state.gate(section.signal).fail("선택되지 않은 트랙")
            return
        state.tracks[name] = TrackStatus.RUNNING
        own = {step.signal for step in section.steps if step.signal}
        if section.signal:
            own.add(section.signal)
        try:
            if target is not None and state.ctx.preparation_errors.get(target.value):
                error = state.ctx.preparation_errors[target.value]
                detail = f"{error['code']}: {error['detail']}"
                sid = f"prepare.infra.{target.value}"
                state.records.append(
                    StepRecord(sid, "apply_infra", target, "failed", 0, error=detail)
                )
                await state.emit(
                    EventType.STEP_FINISHED,
                    step=sid,
                    tool="apply_infra",
                    target=target,
                    status="failed",
                    detail=detail,
                )
                raise DdakToolError(ErrorCode(error["code"]), error["detail"])
            if target is not None and state.ctx.preparation_failures.get(target.value):
                missing = state.ctx.preparation_failures[target.value]
                for step in section.steps:
                    if step.tool in missing:
                        detail = f"CONFIG_INVALID: 준비 실패, 미등록 툴 {step.tool}"
                        state.records.append(
                            StepRecord(step.id, step.tool, target, "failed", 0, error=detail)
                        )
                        await state.emit(
                            EventType.STEP_FINISHED,
                            step=step.id,
                            tool=step.tool,
                            target=target,
                            status="failed",
                            detail=detail,
                        )
                raise DdakToolError(ErrorCode.CONFIG_INVALID, "해당 환경의 준비 실패")
            for step in section.steps:
                if state.stopping:
                    raise asyncio.CancelledError
                for wanted in step.wait_for:
                    gate = state.gate(wanted)
                    if not gate.settled:
                        state.tracks[name] = TrackStatus.WAITING
                        await state.emit(
                            EventType.GATE_WAITING, step=step.id, target=target, detail=wanted
                        )
                    await gate.wait()
                    state.tracks[name] = TrackStatus.RUNNING
                await self._call(step, target, state)
                if step.signal:
                    # 기록 실패 시 대기 중인 변경 작업이 시작되지 않도록 기록 후 신호를 연다.
                    await state.emit(
                        EventType.GATE_OPENED, step=step.id, target=target, detail=step.signal
                    )
                    state.gate(step.signal).open()
            if section.signal:
                await state.emit(EventType.GATE_OPENED, target=target, detail=section.signal)
                state.gate(section.signal).open()
            state.tracks[name] = TrackStatus.DONE
        except GateFailed as exc:
            state.tracks[name] = TrackStatus.SKIPPED
            await state.emit(EventType.GATE_FAILED, target=target, detail=str(exc))
            if target is not None and target in state.app_touched:
                state.tracks[name] = await self._rollback_track(target, state)
        except Exception:
            state.tracks[name] = TrackStatus.FAILED
            if state.event_failed:
                raise  # 필수 기록 실패는 전체 실행을 중지시킨다.
            if target in state.unquiesced:
                state.tracks[name] = TrackStatus.ROLLBACK_FAILED
            elif target is not None and target in state.app_touched:
                state.tracks[name] = await self._rollback_track(target, state)
        finally:
            for signal in own:
                state.gate(signal).fail(f"{name} 트랙이 신호를 열지 못하고 끝났다")

    async def _wait_work(
        self,
        awaitable: Awaitable[Any],
        target: Target | None,
        state: _RunState,
        timeout: float,
        *,
        cancellable: bool = True,
        mutation: bool = False,
    ) -> Any:
        async def invoke() -> Any:
            try:
                return await awaitable
            except DdakToolError as exc:
                if exc.needs_human or (mutation and exc.code is ErrorCode.ADAPTER_TIMEOUT):
                    # 제한 시간/취소 뒤의 정리 유예 중 반환한 timeout도 놓치지 않는다.
                    state.unquiesced.add(target)
                raise

        work = asyncio.create_task(invoke())
        state.work[work] = target

        def finished(task: asyncio.Task[Any]) -> None:
            state.work.pop(task, None)
            _consume_exception(task)

        work.add_done_callback(finished)
        try:
            done, _ = await asyncio.wait({work}, timeout=max(0, timeout))
            if done:
                return work.result()
        except asyncio.CancelledError:
            await self._quiesce(work, target, state, cancellable)
            raise
        await self._quiesce(work, target, state, cancellable)
        raise TimeoutError

    async def _quiesce(
        self, work: asyncio.Task[Any], target: Target | None, state: _RunState, cancellable: bool
    ) -> None:
        if cancellable:
            work.cancel()
        try:
            await asyncio.wait({work}, timeout=_QUIESCE_TIMEOUT_S)
        finally:
            if not work.done():
                state.unquiesced.add(target)

    async def _call(
        self, step: PlanStep, target: Target | None, state: _RunState, *, final: bool = False
    ) -> StepRecord:
        track_target = target
        target = step.target or target
        started = time.monotonic()
        code: ErrorCode
        message: str
        output: dict[str, Any] | None = None
        passed = True
        invoked = False
        changing = False
        try:
            registered = self._registry.get(step.tool)
            spec = registered.spec
            changing = _changes_state(step, spec)
            _check_tool(step, spec, state.ctx)
            deadline = started + spec.timeout_s
            if not final and state.ctx.deadline is not None:
                deadline = min(deadline, state.ctx.deadline)
            async with state.context_lock:
                ctx = replace(state.ctx, deadline=deadline)
                if self._tool_context is not None:
                    ctx = self._tool_context(step, target, ctx)
                inp = build_input(registered, step, track_target, ctx)
                if spec.requires_lock and not ctx.lock_token:
                    raise DdakToolError(
                        ErrorCode.LOCK_INVALID, "잠금이 필요한 툴에 lock_token이 없다"
                    )
                if deadline <= time.monotonic():
                    raise TimeoutError
                if self._before_step is not None:
                    await self._wait_work(
                        self._before_step(step, ctx), target, state, deadline - time.monotonic()
                    )
            if state.stopping and not final:
                raise asyncio.CancelledError
            if final:
                with contextlib.suppress(Exception):
                    await state.emit(
                        EventType.STEP_STARTED, step=step.id, tool=step.tool, target=target
                    )
            else:
                await state.emit(
                    EventType.STEP_STARTED, step=step.id, tool=step.tool, target=target
                )
            if state.stopping and not final:
                raise asyncio.CancelledError
            if deadline <= time.monotonic():
                raise TimeoutError
            if _changes_state(step, spec) and target is not None:
                # before_step이 거부한 작업은 touched가 아니므로 롤백하지 않는다.
                state.touched.add(target)
            invoked = True
            if changing and target is not None and step.tool in {"deploy_tier", "prepare_db"}:
                state.app_touched.add(target)
            if self._on_invoke is not None:
                self._on_invoke(step, ctx)
            with runtime.tool_context(step.tool, ctx.run_id):
                call = (
                    registered.fn(inp, ctx)
                    if registered.is_async
                    else asyncio.to_thread(registered.fn, inp, ctx)
                )
                out = await self._wait_work(
                    call,
                    target,
                    state,
                    deadline - time.monotonic(),
                    cancellable=registered.is_async,
                    mutation=changing,
                )
            if not isinstance(out, registered.output_model):
                raise DdakToolError(ErrorCode.INTERNAL, "출력 모델이 레지스트리와 다르다")
            output = out.model_dump(mode="json")
            passed = (
                getattr(out, "passed", None) is True
                if spec.canonical and step.tool in _VERIFIED_TOOLS
                else getattr(out, "passed", True) is not False
            )
            if passed and step.tool == "apply_infra":
                # 실제 적용 성공은 refresh/기록 후처리 실패와 독립적으로 보존한다.
                state.infra_changes.append(
                    {
                        "step_id": step.id,
                        "target": target.value if target else None,
                        "status": "applied",
                        "plan_sha256": output.get("plan_sha256"),
                    }
                )
            if passed and self._after_step is not None:
                # 툴 실행 시작 때의 ctx를 다시 쓰지 않는다. hook과 교체가 하나의 임계 구역이다.
                async with state.context_lock:
                    if deadline <= time.monotonic():
                        raise TimeoutError
                    updated = await self._wait_work(
                        self._after_step(step, output, state.ctx),
                        target,
                        state,
                        deadline - time.monotonic(),
                    )
                    if (
                        not isinstance(updated, RunContext)
                        or updated.run_id != state.ctx.run_id
                        or updated.targets != state.ctx.targets
                        or updated.trigger != state.ctx.trigger
                    ):
                        raise DdakToolError(
                            ErrorCode.INTERNAL, "after_step 실행 컨텍스트가 잘못됐다"
                        )
                    state.ctx = updated
        except UnknownToolError:
            code, message = ErrorCode.PLAN_INVALID, f"구현이 등록되지 않은 툴: {step.tool}"
        except DdakToolError as exc:
            if exc.needs_human or (invoked and changing and exc.code is ErrorCode.ADAPTER_TIMEOUT):
                # 스레드/CLI 종료 확인만으로 원격 데몬 작업 종료를 증명할 수 없다.
                state.unquiesced.add(target)
            code, message = exc.code, exc.message
        except TimeoutError:
            code, message = ErrorCode.ADAPTER_TIMEOUT, "타임아웃"
        except ValidationError as exc:
            code, message = ErrorCode.PLAN_INVALID, f"입력 검증 실패({exc.error_count()}건)"
        except asyncio.CancelledError:
            self._record_failure(step, target, state, started, "CANCELLED: 실행 취소")
            raise
        except Exception as exc:
            code, message = ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}"
        else:
            record = StepRecord(
                step_id=step.id,
                tool=step.tool,
                target=target,
                status="succeeded" if passed else "check_failed",
                elapsed_s=round(time.monotonic() - started, 3),
                output=output,
            )
            state.records.append(record)
            await state.emit(
                EventType.STEP_FINISHED,
                step=step.id,
                tool=step.tool,
                target=target,
                status=record.status,
                elapsed_s=record.elapsed_s,
            )
            if not passed:
                raise StepFailed(step.id, None, "검사 불합격")
            return record
        if step.tool in {"compare_env_results", "diagnose_parity_gap"}:
            message = "검증 실패(비교 불가): " + message
        message = redact(message)
        record = self._record_failure(step, target, state, started, f"{code.value}: {message}")
        await state.emit(
            EventType.STEP_FINISHED,
            step=step.id,
            tool=step.tool,
            target=target,
            status="failed",
            elapsed_s=record.elapsed_s,
            detail=record.error,
        )
        raise StepFailed(step.id, code, message)

    @staticmethod
    def _record_failure(
        step: PlanStep, target: Target | None, state: _RunState, started: float, error: str
    ) -> StepRecord:
        record = StepRecord(
            step_id=step.id,
            tool=step.tool,
            target=target,
            status="failed",
            elapsed_s=round(time.monotonic() - started, 3),
            error=error,
        )
        state.records.append(record)
        return record

    async def _rollback_track(self, target: Target, state: _RunState) -> TrackStatus:
        if target in state.unquiesced or None in state.unquiesced:
            return TrackStatus.ROLLBACK_FAILED
        if any(owner == target and not task.done() for task, owner in state.work.items()):
            return TrackStatus.ROLLBACK_FAILED
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await state.emit(EventType.ROLLBACK_STARTED, target=target)
        detail: str | None = None
        try:
            if self._rollback is None:
                raise RuntimeError("롤백 훅 없음")
            timeout = self._rollback_timeouts.get(target, _ROLLBACK_TIMEOUT_S)
            await self._wait_work(
                self._rollback(target, replace(state.ctx, deadline=time.monotonic() + timeout)),
                target,
                state,
                timeout,
                mutation=True,
            )
        except (Exception, asyncio.CancelledError) as exc:
            detail = redact(f"{type(exc).__name__}: {exc}")
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await state.emit(
                EventType.ROLLBACK_FINISHED,
                target=target,
                status="failed" if detail else "succeeded",
                detail=detail,
            )
        return TrackStatus.ROLLBACK_FAILED if detail else TrackStatus.ROLLED_BACK


def _consume_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()
