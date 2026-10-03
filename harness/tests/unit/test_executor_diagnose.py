"""원인 분석(diagnose_parity_gap)은 설명 전용 내장 step이다(10/3 결정, 불변 조건 (a)).

- 성공 run에서는 돌지 않는다(SKIPPED 기록).
- compare 불합격이나 환경 트랙 실패 뒤에만 돈다. 롤백 뒤, 보고(finally) 전.
- 출력의 passed·예외·타임아웃은 run 상태·롤백·관문을 바꾸지 않는다.
- 계획에 넣으면 PLAN_INVALID다.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import pytest

from ddak.core.contracts.base import ContractModel, ToolInput
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Layer, Module, Stage, Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.events import EventType, RunEvent
from ddak.core.contracts.plan import Plan, PlanStep
from ddak.core.registry import Registry
from ddak.executor.engine import Executor, RunResult, RunStatus, TrackStatus
from ddak.executor.events import EventBus
from tests.unit.test_executor import REG, Rollbacks, _spec, plan, step

pytestmark = pytest.mark.anyio

DIAGNOSE = "diagnose_parity_gap"


class DiagnoseInput(ToolInput):
    reason: Literal["parity_failed", "track_failed"]
    tracks: dict[str, str]
    failed_steps: list[dict[str, Any]]
    mode: str = "verdict_false"  # verdict_false | raise | slow


class DiagnoseOutput(ContractModel):
    run_id: str
    passed: bool
    summary: str


class MinimalInput(ToolInput):
    """실행기가 주는 필드를 하나도 선언하지 않은 입력도 받아야 한다(선언한 필드만 채움)."""


class Calls:
    def __init__(self) -> None:
        self.inputs: list[ToolInput] = []


def registry(mode: str = "verdict_false", *, timeout_s: int = 5, minimal: bool = False):
    spec = _spec(DIAGNOSE, ()).model_copy(
        update={
            "module": Module.VERIFY,
            "stage": Stage.VERIFY_REPORT,
            "layer": Layer.BUILTIN,
            "uses_ai": True,
            "read_only": True,
            "timeout_s": timeout_s,
        }
    )
    reg = Registry([*REG.specs, spec])
    for name in REG.registered():
        reg.tool(name)(REG.get(name).fn)
    calls = Calls()

    if minimal:

        @reg.tool(DIAGNOSE)
        async def diagnose_min(inp: MinimalInput, ctx: RunContext) -> DiagnoseOutput:
            calls.inputs.append(inp)
            return DiagnoseOutput(run_id=inp.run_id, passed=False, summary="가설")

        return reg, calls

    @reg.tool(DIAGNOSE)
    async def diagnose(inp: DiagnoseInput, ctx: RunContext) -> DiagnoseOutput:
        calls.inputs.append(inp)
        if mode == "raise":
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "password=hunter2 분석 실패")
        if mode == "slow":
            await asyncio.sleep(30)
        return DiagnoseOutput(run_id=inp.run_id, passed=False, summary="설정 차이로 추정")

    return reg, calls


async def run(reg: Registry, p: Plan, events: list[RunEvent] | None = None, **kw: Any):
    bus = EventBus()
    seen = events if events is not None else []

    async def collect(event: RunEvent) -> None:
        seen.append(event)

    bus.subscribe(collect)
    rollbacks = Rollbacks()
    result = await Executor(reg, bus=bus, rollback=rollbacks, **kw).run(p, RunContext("run-1"))
    return result, rollbacks


def diagnose_records(result: RunResult) -> list[Any]:
    return [r for r in result.records if r.tool == DIAGNOSE]


async def test_success_run_skips_diagnose_even_if_registered_with_false_verdict() -> None:
    reg, calls = registry()
    events: list[RunEvent] = []
    result, rollbacks = await run(reg, plan(report_mode="ok"), events)
    assert result.status is RunStatus.SUCCEEDED
    assert set(result.tracks.values()) == {TrackStatus.DONE}
    assert rollbacks.targets == []
    assert calls.inputs == []  # 성공 run에서는 부르지 않는다
    assert [(r.step_id, r.status) for r in diagnose_records(result)] == [
        ("verify.diagnose", "skipped")
    ]
    assert any(e.type is EventType.STEP_SKIPPED and e.step == "verify.diagnose" for e in events)


@pytest.mark.parametrize("registered", [False, True])
async def test_parity_status_comes_from_compare_only(registered: bool) -> None:
    reg, calls = registry() if registered else (REG, Calls())
    result, rollbacks = await run(reg, plan(compare_mode="check_fail", report_mode="ok"))
    # diagnose 등록 여부와 무관하게 compare 불합격만으로 같은 판정·롤백이 나온다.
    assert result.status is RunStatus.PARITY_FAILED
    assert result.tracks["local"] is TrackStatus.DONE
    assert result.tracks["cloud"] is TrackStatus.ROLLED_BACK
    assert rollbacks.targets == [Target.CLOUD]
    if not registered:
        assert diagnose_records(result) == []
        return
    [record] = diagnose_records(result)
    # passed=False를 돌려줘도 기록은 succeeded(설명 산출 성공)이고 출력에만 남는다.
    assert record.status == "succeeded" and record.output["passed"] is False
    [inp] = calls.inputs
    assert isinstance(inp, DiagnoseInput)
    assert inp.reason == "parity_failed"
    assert inp.tracks["cloud"] == "ROLLED_BACK"
    compare = [s for s in inp.failed_steps if s["tool"] == "compare_env_results"]
    assert compare and compare[0]["status"] == "check_failed"
    assert compare[0]["output"] == {"passed": False}


async def test_diagnose_runs_after_track_failure_without_changing_status() -> None:
    reg, calls = registry()
    events: list[RunEvent] = []
    result, rollbacks = await run(reg, plan(local_mode="raise", report_mode="ok"), events)
    assert result.status is RunStatus.FAILED_LOCAL
    assert result.tracks["local"] is TrackStatus.ROLLED_BACK
    assert result.tracks["cloud"] is TrackStatus.DONE
    assert rollbacks.targets == [Target.LOCAL]
    [inp] = calls.inputs
    assert isinstance(inp, DiagnoseInput) and inp.reason == "track_failed"
    [failed] = [s for s in inp.failed_steps if s["step_id"] == "deploy.was.local"]
    assert failed["target"] == "local" and failed["error"].startswith("ADAPTER_FAILED")
    assert "hunter2" not in failed["error"]  # 실행기 기록 그대로(이미 redact)
    # 순서: 롤백 끝 → 원인 분석 → 보고(finally)
    seqs = {
        "rollback": next(e.seq for e in events if e.type is EventType.ROLLBACK_FINISHED),
        "diagnose": next(
            e.seq
            for e in events
            if e.type is EventType.STEP_STARTED and e.step == "verify.diagnose"
        ),
        "report": next(
            e.seq for e in events if e.type is EventType.STEP_STARTED and e.step == "verify.report"
        ),
    }
    assert seqs["rollback"] < seqs["diagnose"] < seqs["report"]
    final = [e for e in events if e.type is EventType.RUN_STATE][-1]
    assert final.status == "FAILED_LOCAL"


@pytest.mark.parametrize(("mode", "timeout_s"), [("raise", 5), ("slow", 1)])
async def test_diagnose_failure_or_timeout_is_only_a_warning(mode: str, timeout_s: int) -> None:
    reg, _ = registry(mode, timeout_s=timeout_s)
    p = plan(compare_mode="check_fail", report_mode="ok")
    result, rollbacks = await run(reg, p)
    assert result.status is RunStatus.PARITY_FAILED
    assert rollbacks.targets == [Target.CLOUD]
    [record] = diagnose_records(result)
    assert record.status == "failed"
    assert record.error is not None and "판정 영향 없음" in record.error
    assert "hunter2" not in record.error
    report = [r for r in result.records if r.step_id == "verify.report"]
    assert [r.status for r in report] == ["succeeded"]  # 보고는 그대로 돈다


async def test_diagnose_passes_only_fields_declared_by_input_model() -> None:
    reg, calls = registry(minimal=True)
    result, _ = await run(reg, plan(compare_mode="check_fail"))
    assert result.status is RunStatus.PARITY_FAILED
    assert [type(i) for i in calls.inputs] == [MinimalInput]
    assert [r.status for r in diagnose_records(result)] == ["succeeded"]


async def test_diagnose_output_does_not_reach_after_step() -> None:
    reg, calls = registry()
    seen: list[str] = []

    async def after(s: PlanStep, output: dict[str, Any], ctx: RunContext) -> RunContext:
        seen.append(s.id)
        return ctx

    result, _ = await run(reg, plan(compare_mode="check_fail"), after_step=after)
    assert result.status is RunStatus.PARITY_FAILED
    assert len(calls.inputs) == 1
    assert "verify.diagnose" not in seen  # AI 설명은 실행 컨텍스트를 바꾸지 않는다


@pytest.mark.parametrize("tool", [DIAGNOSE, "t_step"])
async def test_diagnose_in_plan_is_rejected(tool: str) -> None:
    reg, calls = registry()
    p = plan()
    sid = "verify.diagnose" if tool == "t_step" else "verify.cause"
    p.verify.steps.append(step(sid, tool=tool))
    with pytest.raises(DdakToolError) as caught:
        await run(reg, p)
    assert caught.value.code is ErrorCode.PLAN_INVALID
    assert calls.inputs == []
