"""실행기 최소 예시(O1 정준우): plan.json 순서대로 레지스트리 함수를 직접 호출한다. AI 없음.

- 빌드·로컬 트랙·클라우드 트랙을 동시에 시작한다(✅ 장부 1·6: 시작은 동시에, 병렬로 진행).
- wait_for가 있는 step은 그 신호가 열릴 때까지 기다린다. 대표 대기 지점은 local_verified(G1).
- 로컬이 실패하면 local_verified가 실패로 닫히고 클라우드는 대기 지점에서 멈춘다(ABORTED_AT_GATE).
- step이 실패하면 그 트랙(환경)만 규칙 롤백(앱만, 이전 digest). 상태를 바꾸는 step을 시작했을 때만.
- 검증 섹션(compare 등)은 트랙이 모두 끝난 뒤 실행한다. 필요한 신호가 없으면 대기 지점에서 멈춘다.
- run="finally" step(verify.report = post_report)은 앞이 실패해도 마지막에 실행한다.
  보고가 실패해도 run 결과는 바꾸지 않는다(설계 문서 02 8-3).
- 툴은 이름으로만 찾는다(REGISTRY.get). 실행기는 툴 모듈을 import하지 않는다(import-linter 계약 4).

TODO(O1 정준우): 배포 잠금(acquire_deploy_lock, SQLite locks, 하트비트), plan_hash 대조(V12),
          facts 재계산(V11), 상태 DB(runs·steps·env_release), events.jsonl 기록,
          실패 시 collect_diagnostics -> diagnose_parity_gap(AI 설명), finally에서
          record_deploy_log, request_approval(배포 클릭) 대기, 실제 rollback_tier 연결.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from ddak.core import runtime
from ddak.core.contracts.base import ToolInput
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Effect, Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.events import EventType, RunEvent
from ddak.core.contracts.plan import CLOUD_VERIFIED, LOCAL_VERIFIED, Plan, PlanStep, Section
from ddak.core.redact import redact
from ddak.core.registry import RegisteredTool, Registry, UnknownToolError
from ddak.executor.events import EventBus

TRACK_BUILD, TRACK_LOCAL, TRACK_CLOUD, TRACK_VERIFY = "build", "local", "cloud", "verify"
_RESERVED_PARAMS = frozenset({"run_id", "target", "tier", "lock_token"})

RollbackHook = Callable[[Target, RunContext], Awaitable[None]]


class TrackStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    DONE = "DONE"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    ABORTED_AT_GATE = "ABORTED_AT_GATE"  # 기다리던 신호가 실패로 닫힘(롤백할 것 없음)


class RunStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED_BEFORE_DEPLOY = "FAILED_BEFORE_DEPLOY"  # 빌드 실패 등. 대상 환경 무변경
    FAILED_LOCAL = "FAILED_LOCAL"  # 온프렘 롤백, 클라우드 무변경(관문 작동)
    FAILED_CLOUD = "FAILED_CLOUD"  # 클라우드 롤백, 온프렘은 새 버전 유지(DIVERGED 표시)
    PARITY_FAILED = "PARITY_FAILED"  # 교차 검증 불일치(처리 방식은 D10, 미결)
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


class _RunState:
    def __init__(self, ctx: RunContext, bus: EventBus | None) -> None:
        self.ctx = ctx
        self.bus = bus
        self.gates: dict[str, Gate] = {}
        self.tracks: dict[str, TrackStatus] = {}
        self.records: list[StepRecord] = []
        self._seq = 0

    def gate(self, name: str) -> Gate:
        return self.gates.setdefault(name, Gate(name))

    async def emit(self, type_: EventType, **fields: Any) -> None:
        event = RunEvent(
            run_id=self.ctx.run_id,
            seq=self._seq,
            type=type_,
            ts=datetime.now(UTC).isoformat(timespec="milliseconds"),
            **fields,
        )
        self._seq += 1
        if self.bus is not None:
            await self.bus.publish(event)


# ---------------------------------------------------------------------------
# 계획 모양 검사(방어용 중복). 1차 관문은 validate_plan(V8)이다.
# ---------------------------------------------------------------------------
def check_signals(plan: Plan) -> None:
    sections = {
        TRACK_BUILD: plan.build,
        TRACK_LOCAL: plan.deploy.local,
        TRACK_CLOUD: plan.deploy.cloud,
        TRACK_VERIFY: plan.verify,
    }
    produced: set[str] = set()
    for name, section in sections.items():
        produced |= {s.signal for s in section.steps if s.signal}
        if section.signal:
            produced.add(section.signal)
        for step in section.steps:
            if step.run is None:
                continue
            if name != TRACK_VERIFY:
                msg = f"run=finally step은 verify 섹션에만 둔다: {step.id}"
                raise DdakToolError(ErrorCode.PLAN_INVALID, msg)
            if step.wait_for or step.signal:
                msg = f"run=finally step은 신호를 기다리거나 열지 않는다: {step.id}"
                raise DdakToolError(ErrorCode.PLAN_INVALID, msg)
    for name, section in sections.items():
        later = {s.signal for s in section.steps if s.signal} | (
            {section.signal} if section.signal else set()
        )
        for step in section.steps:
            for wanted in step.wait_for:
                if wanted not in produced:
                    msg = f"아무도 열지 않는 신호를 기다린다: {step.id} -> {wanted}"
                    raise DdakToolError(ErrorCode.PLAN_INVALID, msg)
                if wanted in later:
                    msg = f"같은 트랙의 뒤 신호를 기다린다(교착): {step.id} -> {wanted}"
                    raise DdakToolError(ErrorCode.PLAN_INVALID, msg)
            if step.signal:
                later.discard(step.signal)
        if name == TRACK_LOCAL and any(CLOUD_VERIFIED in s.wait_for for s in section.steps):
            raise DdakToolError(
                ErrorCode.PLAN_INVALID, "로컬 트랙은 클라우드를 기다리지 않는다(W3)"
            )
    # W1: 클라우드 트랙의 상태 변경 step은 local_verified 대기 뒤에만 온다.
    gated = False
    for step in plan.deploy.cloud.steps:
        gated = gated or LOCAL_VERIFIED in step.wait_for
        if step.effect is Effect.STATE_CHANGE and not gated:
            msg = f"클라우드 상태 변경 step은 local_verified 대기 뒤에 둔다(W1): {step.id}"
            raise DdakToolError(ErrorCode.PLAN_INVALID, msg)


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
    if local in bad:
        return RunStatus.FAILED_LOCAL
    if cloud in bad:
        return RunStatus.FAILED_CLOUD
    if tracks.get(TRACK_BUILD) in bad or TrackStatus.ABORTED_AT_GATE in (local, cloud):
        return RunStatus.FAILED_BEFORE_DEPLOY
    verify = tracks.get(TRACK_VERIFY)
    if verify in bad or verify is TrackStatus.ABORTED_AT_GATE:
        return RunStatus.PARITY_FAILED
    return RunStatus.SUCCEEDED


class Executor:
    def __init__(
        self,
        registry: Registry,
        *,
        bus: EventBus | None = None,
        rollback: RollbackHook | None = None,
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._rollback = rollback

    async def run(self, plan: Plan, ctx: RunContext) -> RunResult:
        if plan.run_id != ctx.run_id:
            raise DdakToolError(ErrorCode.PLAN_INVALID, "plan.run_id와 실행 컨텍스트가 다르다")
        check_signals(plan)
        state = _RunState(ctx, self._bus)
        await state.emit(EventType.RUN_STATE, status="DEPLOYING")
        # _track은 예외를 밖으로 내보내지 않는다(트랙 상태로 기록). 취소(CancelledError)만 전파된다.
        await asyncio.gather(
            self._track(TRACK_BUILD, plan.build, None, state),
            self._track(TRACK_LOCAL, plan.deploy.local, Target.LOCAL, state),
            self._track(TRACK_CLOUD, plan.deploy.cloud, Target.CLOUD, state),
        )
        main = [s for s in plan.verify.steps if s.run is None]
        final = [s for s in plan.verify.steps if s.run == "finally"]
        await self._track(TRACK_VERIFY, plan.verify.model_copy(update={"steps": main}), None, state)
        status = decide_status(state.tracks)
        for step in final:  # 보고(verify.report): 앞이 실패해도 실행, 실패해도 결과는 그대로
            with contextlib.suppress(StepFailed):
                await self._call(step, None, state)
        await state.emit(EventType.RUN_STATE, status=status.value)
        return RunResult(
            run_id=ctx.run_id,
            status=status,
            tracks=dict(state.tracks),
            records=list(state.records),
            gates={name: gate.ok for name, gate in state.gates.items()},
        )

    async def _track(
        self, name: str, section: Section, target: Target | None, state: _RunState
    ) -> None:
        state.tracks[name] = TrackStatus.RUNNING
        own = {s.signal for s in section.steps if s.signal}
        if section.signal:
            own.add(section.signal)
        touched = False
        try:
            for step in section.steps:
                for wanted in step.wait_for:
                    gate = state.gate(wanted)
                    if not gate.settled:
                        state.tracks[name] = TrackStatus.WAITING
                        await state.emit(
                            EventType.GATE_WAITING, step=step.id, target=target, detail=wanted
                        )
                    await gate.wait()
                    state.tracks[name] = TrackStatus.RUNNING
                # 상태를 바꾸는 step은 시작만 해도(실패했더라도) 롤백 대상이다.
                touched = touched or step.effect is Effect.STATE_CHANGE
                await self._call(step, target, state)
                if step.signal:
                    state.gate(step.signal).open()
                    await state.emit(
                        EventType.GATE_OPENED, step=step.id, target=target, detail=step.signal
                    )
            if section.signal:
                state.gate(section.signal).open()
                await state.emit(EventType.GATE_OPENED, target=target, detail=section.signal)
            state.tracks[name] = TrackStatus.DONE
        except GateFailed as exc:
            state.tracks[name] = TrackStatus.ABORTED_AT_GATE
            await state.emit(EventType.GATE_FAILED, target=target, detail=str(exc))
        except Exception:
            state.tracks[name] = TrackStatus.FAILED
            if touched and target is not None:
                state.tracks[name] = await self._rollback_track(target, state)
        finally:
            for signal in own:  # 열지 못한 신호는 실패로 닫는다(기다리는 트랙이 멈추지 않게)
                state.gate(signal).fail(f"{name} 트랙이 신호를 열지 못하고 끝났다")

    async def _call(self, step: PlanStep, target: Target | None, state: _RunState) -> StepRecord:
        ctx = state.ctx
        started = time.monotonic()
        await state.emit(EventType.STEP_STARTED, step=step.id, tool=step.tool, target=target)
        code: ErrorCode
        message: str
        try:
            registered = self._registry.get(step.tool)
            inp = build_input(registered, step, target, ctx)
            with runtime.tool_context(step.tool, ctx.run_id):
                if registered.is_async:
                    coro = registered.fn(inp, ctx)
                else:  # blocking 툴(boto3, docker)은 워커 스레드에서. contextvar는 복사된다
                    coro = asyncio.to_thread(registered.fn, inp, ctx)
                out = await asyncio.wait_for(coro, timeout=registered.spec.timeout_s)
            if not isinstance(out, registered.output_model):
                raise DdakToolError(ErrorCode.INTERNAL, "출력 모델이 레지스트리와 다르다")
        except UnknownToolError:
            code, message = ErrorCode.PLAN_INVALID, f"구현이 등록되지 않은 툴: {step.tool}"
        except DdakToolError as exc:
            code, message = exc.code, exc.message
        except TimeoutError:
            code, message = ErrorCode.ADAPTER_TIMEOUT, "타임아웃"
        except ValidationError as exc:
            code, message = ErrorCode.PLAN_INVALID, f"입력 검증 실패({exc.error_count()}건)"
        except Exception as exc:
            code, message = ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}"
        else:
            passed = getattr(out, "passed", True) is not False
            record = StepRecord(
                step_id=step.id,
                tool=step.tool,
                target=target,
                status="succeeded" if passed else "check_failed",
                elapsed_s=round(time.monotonic() - started, 3),
                output=out.model_dump(mode="json"),
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
        message = redact(message)
        state.records.append(
            StepRecord(
                step_id=step.id,
                tool=step.tool,
                target=target,
                status="failed",
                elapsed_s=round(time.monotonic() - started, 3),
                error=f"{code.value}: {message}",
            )
        )
        await state.emit(
            EventType.STEP_FINISHED,
            step=step.id,
            tool=step.tool,
            target=target,
            status="failed",
            detail=f"{code.value}: {message}",
        )
        raise StepFailed(step.id, code, message)

    async def _rollback_track(self, target: Target, state: _RunState) -> TrackStatus:
        await state.emit(EventType.ROLLBACK_STARTED, target=target)
        if self._rollback is None:
            detail = "롤백 훅 없음(TODO(O1): rollback_tier 연결)"
            await state.emit(
                EventType.ROLLBACK_FINISHED, target=target, status="failed", detail=detail
            )
            return TrackStatus.ROLLBACK_FAILED
        try:
            await self._rollback(target, state.ctx)
        except Exception as exc:
            detail = redact(f"{type(exc).__name__}: {exc}")
            await state.emit(
                EventType.ROLLBACK_FINISHED, target=target, status="failed", detail=detail
            )
            return TrackStatus.ROLLBACK_FAILED
        await state.emit(EventType.ROLLBACK_FINISHED, target=target, status="succeeded")
        return TrackStatus.ROLLED_BACK
