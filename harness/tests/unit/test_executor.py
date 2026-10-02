"""실행기 최소 예시: 트랙 병렬 + wait_for/signal 대기 지점 + 트랙별 규칙 롤백.

테스트용 카탈로그와 레지스트리를 따로 만든다(실제 툴 구현과 무관하게 실행기 규칙만 본다).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ddak.core.contracts.base import ContractModel, ToolInput
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Effect, Layer, Module, Stage, Target, ToolKind
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.events import EventType, RunEvent
from ddak.core.contracts.plan import Plan, PlanStep
from ddak.core.registry import Registry, ToolSpec
from ddak.executor.engine import Executor, RunStatus, TrackStatus, check_signals
from ddak.executor.events import EventBus

pytestmark = pytest.mark.anyio

LOG: list[str] = []


def _spec(name: str, targets: tuple[Target, ...] = (Target.LOCAL, Target.CLOUD)) -> ToolSpec:
    return ToolSpec(
        name=name,
        module=Module.CORE,
        stage=Stage.DEPLOY,
        kind=ToolKind.TOOL_FN,
        uses_ai=False,
        layer=Layer.CONDITIONAL,
        targets=targets,
        owners={"logic": "O1"},
        timeout_s=5,
    )


class TInput(ToolInput):
    target: Target | None = None
    mode: str = "ok"  # ok | raise | check_fail
    delay: float = 0.0


class TOutput(ContractModel):
    passed: bool


REG = Registry(
    [
        _spec("t_step"),
        _spec("t_cloud_only", (Target.CLOUD,)),
        _spec("t_missing"),
        _spec("deploy_tier"),
        _spec("compare_env_results"),
    ]
)


@REG.tool("deploy_tier")
@REG.tool("compare_env_results")
@REG.tool("t_step")
async def t_step(inp: TInput, ctx: RunContext) -> TOutput:
    LOG.append(f"start:{inp.target}:{inp.mode}")
    await asyncio.sleep(inp.delay)
    if inp.mode == "raise":
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "password=hunter2 연결 실패")
    LOG.append(f"end:{inp.target}")
    return TOutput(passed=inp.mode != "check_fail")


@REG.tool("t_cloud_only")
def t_cloud_only(inp: TInput, ctx: RunContext) -> TOutput:  # 동기 툴: 워커 스레드에서 실행된다
    return TOutput(passed=True)


def step(sid: str, tool: str = "t_step", **kw: Any) -> PlanStep:
    if tool == "t_step" and kw.get("effect") is Effect.STATE_CHANGE:
        tool = "deploy_tier"
    params = {k: kw.pop(k) for k in ("mode", "delay") if k in kw}
    return PlanStep(id=sid, tool=tool, layer=Layer.CONDITIONAL, params=params, **kw)


def plan(
    *,
    local_mode: str = "ok",
    cloud_mode: str = "ok",
    compare_mode: str = "ok",
    report_mode: str | None = None,  # 주면 verify.report(run=finally) step을 붙인다
) -> Plan:
    verify_steps = [
        step(
            "verify.compare",
            tool="compare_env_results",
            wait_for=["local_verified", "cloud_verified"],
            mode=compare_mode,
        )
    ]
    if report_mode is not None:
        verify_steps.append(step("verify.report", run="finally", mode=report_mode))
    return Plan.model_validate(
        {
            "run_id": "run-1",
            "build": {"steps": [step("build.was", delay=0.01)], "signal": "images_ready"},
            "deploy": {
                "local": {
                    "steps": [
                        step(
                            "deploy.was.local",
                            wait_for=["images_ready"],
                            mode=local_mode,
                            effect=Effect.STATE_CHANGE,
                            delay=0.02,
                        ),
                        step("verify.smoke.local", signal="local_verified", delay=0.02),
                    ]
                },
                "cloud": {
                    "steps": [
                        step("deploy.tls.cloud", tool="t_cloud_only"),  # 읽기: G1 전 병렬
                        step(
                            "deploy.app.cloud",
                            wait_for=["images_ready"],
                            mode=cloud_mode,
                            effect=Effect.STATE_CHANGE,
                        ),
                        step("verify.smoke.cloud", signal="cloud_verified"),
                    ]
                },
            },
            "verify": {"steps": verify_steps},
        }
    )


class Rollbacks:
    def __init__(self) -> None:
        self.targets: list[Target] = []

    async def __call__(self, target: Target, ctx: RunContext) -> None:
        self.targets.append(target)


def _executor(rollbacks: Rollbacks, events: list[RunEvent]) -> Executor:
    bus = EventBus()

    async def collect(event: RunEvent) -> None:
        events.append(event)

    bus.subscribe(collect)
    return Executor(REG, bus=bus, rollback=rollbacks)


async def test_tracks_start_together_and_cloud_does_not_wait_for_local_verified() -> None:
    LOG.clear()
    events: list[RunEvent] = []
    rollbacks = Rollbacks()
    result = await _executor(rollbacks, events).run(plan(), RunContext(run_id="run-1"))
    assert result.status is RunStatus.SUCCEEDED
    assert set(result.tracks.values()) == {TrackStatus.DONE}
    ids = [e.step for e in events if e.type is EventType.STEP_STARTED]
    # 클라우드 읽기 step은 빌드·로컬과 동시에 시작한다(로컬 검증 전).
    assert ids.index("deploy.tls.cloud") < ids.index("verify.smoke.local")
    # 클라우드는 빌드 완료 뒤 로컬 검증을 기다리지 않고 시작한다.
    opened = next(
        e.seq for e in events if e.type is EventType.GATE_OPENED and e.detail == "local_verified"
    )
    app_cloud = next(
        e.seq for e in events if e.type is EventType.STEP_STARTED and e.step == "deploy.app.cloud"
    )
    assert app_cloud < opened
    assert any(e.type is EventType.GATE_WAITING for e in events)
    assert result.gates == {"images_ready": True, "local_verified": True, "cloud_verified": True}
    assert rollbacks.targets == []


async def test_local_failure_rolls_back_local_only_and_preserves_cloud_success() -> None:
    events: list[RunEvent] = []
    rollbacks = Rollbacks()
    result = await _executor(rollbacks, events).run(plan(local_mode="raise"), RunContext("run-1"))
    assert result.status is RunStatus.FAILED_LOCAL
    assert result.tracks["local"] is TrackStatus.ROLLED_BACK
    assert result.tracks["cloud"] is TrackStatus.DONE
    assert result.tracks["verify"] is TrackStatus.SKIPPED
    assert rollbacks.targets == [Target.LOCAL]
    started = {e.step for e in events if e.type is EventType.STEP_STARTED}
    assert "deploy.tls.cloud" in started  # 대기 지점 전 읽기는 이미 돌았다
    assert "deploy.app.cloud" in started  # 상태 변경은 시작하지 않았다
    failed = next(r for r in result.records if r.status == "failed")
    assert failed.error is not None and failed.error.startswith("ADAPTER_FAILED")
    assert "hunter2" not in failed.error  # 오류 메시지도 redact


async def test_cloud_failure_rolls_back_cloud_only() -> None:
    rollbacks = Rollbacks()
    result = await _executor(rollbacks, []).run(plan(cloud_mode="raise"), RunContext("run-1"))
    assert result.status is RunStatus.FAILED_CLOUD
    assert result.tracks["local"] is TrackStatus.DONE  # 온프렘은 새 버전 유지(DIVERGED 표시)
    assert result.tracks["cloud"] is TrackStatus.ROLLED_BACK
    assert rollbacks.targets == [Target.CLOUD]


async def test_check_failure_is_a_failure_and_rollback_missing_needs_human() -> None:
    executor = Executor(REG)  # 롤백 훅 없음
    result = await executor.run(plan(local_mode="check_fail"), RunContext("run-1"))
    assert result.tracks["local"] is TrackStatus.ROLLBACK_FAILED
    assert result.status is RunStatus.NEEDS_HUMAN


async def test_compare_failure_is_parity_failed() -> None:
    rollbacks = Rollbacks()
    result = await Executor(REG, rollback=rollbacks).run(
        plan(compare_mode="check_fail"), RunContext("run-1")
    )
    assert result.status is RunStatus.PARITY_FAILED
    assert result.tracks["local"] is TrackStatus.DONE
    assert result.tracks["cloud"] is TrackStatus.ROLLED_BACK
    assert rollbacks.targets == [Target.CLOUD]


async def test_finally_report_runs_after_local_failure_and_keeps_status() -> None:
    # 보고(verify.report, run=finally)는 로컬이 실패해 compare가 대기 지점에서 멈춰도 실행된다.
    result = await _executor(Rollbacks(), []).run(
        plan(local_mode="raise", report_mode="ok"), RunContext("run-1")
    )
    assert result.status is RunStatus.FAILED_LOCAL
    assert result.tracks["verify"] is TrackStatus.SKIPPED
    report = [r for r in result.records if r.step_id == "verify.report"]
    assert [r.status for r in report] == ["succeeded"]


async def test_finally_report_failure_does_not_change_run_status() -> None:
    result = await Executor(REG).run(plan(report_mode="raise"), RunContext("run-1"))
    assert result.status is RunStatus.SUCCEEDED  # 보고 실패 = 결과 유지(규칙 카드만)
    report = [r for r in result.records if r.step_id == "verify.report"]
    assert [r.status for r in report] == ["failed"]


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        # finally는 verify 섹션에만
        ({"deploy": {"local": {"steps": [step("deploy.x.local", run="finally")]}}}, "verify 섹션"),
        # finally step은 신호를 기다리지 않는다(앞이 실패해도 돌아야 하므로)
        (
            {
                "build": {"steps": [step("build.was")], "signal": "images_ready"},
                "verify": {
                    "steps": [step("verify.report", run="finally", wait_for=["images_ready"])]
                },
            },
            "신호",
        ),
    ],
)
async def test_finally_step_shape_is_checked(data: dict[str, Any], fragment: str) -> None:
    p = Plan.model_validate({"run_id": "run-1", **data})
    with pytest.raises(DdakToolError) as info:
        check_signals(p)
    assert fragment in info.value.message


async def test_cloud_only_tool_in_local_track_is_rejected() -> None:
    # ensure_tls·verify_tls처럼 cloud만 대상인 툴을 local 트랙에 넣으면 실행기도 거부한다.
    p = Plan.model_validate(
        {
            "run_id": "run-1",
            "deploy": {"local": {"steps": [step("deploy.tls.local", tool="t_cloud_only")]}},
        }
    )
    result = await Executor(REG).run(p, RunContext("run-1"))
    assert result.tracks["local"] is TrackStatus.FAILED
    error = result.records[0].error or ""
    assert error.startswith("PLAN_INVALID")
    assert "대상이 없다" in error


async def test_unregistered_tool_fails_with_plan_invalid() -> None:
    p = Plan.model_validate(
        {
            "run_id": "run-1",
            "deploy": {"local": {"steps": [step("deploy.x.local", tool="t_missing")]}},
        }
    )
    result = await Executor(REG).run(p, RunContext("run-1"))
    assert result.tracks["local"] is TrackStatus.FAILED
    assert (result.records[0].error or "").startswith("PLAN_INVALID")


@pytest.mark.parametrize(
    ("deploy", "fragment"),
    [
        # 아무도 열지 않는 신호를 기다린다
        ({"local": {"steps": [step("deploy.was.local", wait_for=["images_ready"])]}}, "아무도"),
        # 같은 트랙의 뒤 신호를 기다린다(교착)
        (
            {
                "local": {
                    "steps": [
                        step("a.b.local", wait_for=["local_verified"]),
                        step("c.d.local", signal="local_verified"),
                    ]
                }
            },
            "교착",
        ),
    ],
)
async def test_plan_shape_is_checked(deploy: dict[str, Any], fragment: str) -> None:
    p = Plan.model_validate({"run_id": "run-1", "deploy": deploy})
    with pytest.raises(DdakToolError) as info:
        check_signals(p)
    assert info.value.code is ErrorCode.PLAN_INVALID
    assert fragment in info.value.message


def bootstrap_plan(*, infra_mode: str = "ok") -> Plan:
    """부트스트랩 모양(💭): 클라우드 트랙 첫 step이 인프라 apply(infra_ready).

    CodeBuild 프로젝트(ECR 옵션이면 ECR도)가 apply로 생기므로 빌드는 infra_ready 뒤다.
    create뿐인 apply는 서비스 중인 것을 바꾸지 않으므로 additive_prep(W2)로 G1 전에 돈다.
    """
    return Plan.model_validate(
        {
            "run_id": "run-1",
            "mode": "bootstrap",
            "build": {
                "steps": [step("build.was", wait_for=["infra_ready"], delay=0.01)],
                "signal": "images_ready",
            },
            "deploy": {
                "local": {
                    "steps": [
                        step(
                            "deploy.was.local",
                            wait_for=["images_ready"],
                            effect=Effect.STATE_CHANGE,
                        ),
                        step("verify.smoke.local", signal="local_verified"),
                    ]
                },
                "cloud": {
                    "steps": [
                        step(
                            "deploy.infra.cloud",
                            mode=infra_mode,
                            effect=Effect.ADDITIVE_PREP,
                            signal="infra_ready",
                            delay=0.01,
                        ),
                        step(
                            "deploy.app.cloud",
                            wait_for=["images_ready"],
                            effect=Effect.STATE_CHANGE,
                        ),
                        step("verify.smoke.cloud", signal="cloud_verified"),
                    ]
                },
            },
            "verify": {
                "steps": [step("verify.compare", wait_for=["local_verified", "cloud_verified"])]
            },
        }
    )


async def test_bootstrap_build_waits_for_infra_ready() -> None:
    events: list[RunEvent] = []
    result = await _executor(Rollbacks(), events).run(bootstrap_plan(), RunContext("run-1"))
    assert result.status is RunStatus.SUCCEEDED
    assert result.gates["infra_ready"] is True
    infra_opened = next(
        e.seq for e in events if e.type is EventType.GATE_OPENED and e.detail == "infra_ready"
    )
    build_started = next(
        e.seq for e in events if e.type is EventType.STEP_STARTED and e.step == "build.was"
    )
    assert infra_opened < build_started


async def test_bootstrap_infra_failure_stops_build_and_local_at_gate() -> None:
    events: list[RunEvent] = []
    rollbacks = Rollbacks()
    result = await _executor(rollbacks, events).run(
        bootstrap_plan(infra_mode="raise"), RunContext("run-1")
    )
    assert result.tracks["cloud"] is TrackStatus.FAILED
    assert result.tracks["build"] is TrackStatus.SKIPPED
    assert result.tracks["local"] is TrackStatus.SKIPPED
    started = {e.step for e in events if e.type is EventType.STEP_STARTED}
    assert "build.was" not in started  # 인프라가 없으면 빌드·배포를 시작하지 않는다
    assert rollbacks.targets == []  # additive_prep만 시작했으므로 앱 롤백 대상이 아니다


async def test_single_environment_verify_failure_is_not_parity_failure():
    p = plan()
    p.verify.steps[:] = [step("verify.watch.cloud", target=Target.CLOUD, mode="check_fail")]
    rollback = Rollbacks()
    result = await Executor(REG, rollback=rollback).run(p, RunContext("run-1"))
    assert result.status is RunStatus.FAILED_CLOUD
    assert rollback.targets == [Target.CLOUD]
    assert result.tracks["local"] is TrackStatus.DONE


async def test_compare_condition_emits_skipped_event_and_record():
    p = plan(local_mode="raise")
    events = []
    result = await _executor(Rollbacks(), events).run(p, RunContext("run-1"))
    assert any(r.step_id == "verify.compare" and r.status == "skipped" for r in result.records)
    assert any(e.type is EventType.STEP_SKIPPED and e.step == "verify.compare" for e in events)


async def test_local_verify_failure_still_runs_cloud_verify_and_skips_compare():
    p = plan()
    p.verify.steps[:0] = [
        step("verify.watch.local", target=Target.LOCAL, mode="check_fail"),
        step("verify.watch.cloud", target=Target.CLOUD),
    ]
    events = []
    rollback = Rollbacks()
    result = await _executor(rollback, events).run(p, RunContext("run-1"))
    assert result.status is RunStatus.FAILED_LOCAL
    assert rollback.targets == [Target.LOCAL]
    assert (
        next(r for r in result.records if r.step_id == "verify.watch.cloud").status == "succeeded"
    )
    assert next(r for r in result.records if r.step_id == "verify.compare").status == "skipped"
    assert any(e.type is EventType.STEP_SKIPPED and e.step == "verify.compare" for e in events)
