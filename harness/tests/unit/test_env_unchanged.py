"""env_unchanged: 변경 없는 환경을 뺀 계획의 대상 선택과 실행 결과.

데스크톱 run-20261003-153406-7d35(targets=both): 온프레미스가 직전 성공 배포와 같아
health·smoke만 남았고, 넘겨받을 이미지가 없어 health가 실패했다(FAILED_LOCAL).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ddak.core.contracts.base import ContractModel, ToolInput
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode, Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Plan, Section
from ddak.core.contracts.plan_facts import Facts
from ddak.core.contracts.tools.validate_plan import ValidatePlanInput
from ddak.core.registry import Registry, spec_for
from ddak.executor.engine import Executor, RunStatus, TrackStatus
from ddak.executor.selection import select_plan
from ddak.plan.validate import validate_plan

H = "sha256:" + "a" * 64


def _plan(local_changed: bool = False, cloud_changed: bool = True) -> Plan:
    facts = Facts(
        project="flaskr", mode=RunMode.UPDATE, target="both", tiers=("was", "web"),
        changed={
            "local": {"was": local_changed, "web": False},
            "cloud": {"was": cloud_changed, "web": False},
        },
        db_initialized={"local": True, "cloud": True}, source_snapshot_hash=H, facts_hash=H,
    )  # fmt: skip
    return validate_plan(
        ValidatePlanInput(run_id="run-1", facts=facts),
        RunContext("run-1"),
        registered_tools=set(),
    )


def test_both_allows_section_excluded_as_unchanged() -> None:
    plan = _plan()
    assert select_plan(plan, RunContext("run-1", targets="both")) is plan


def test_both_rejects_section_empty_for_other_reasons() -> None:
    plan = _plan()
    # 같은 빈 섹션이라도 env_unchanged로 뺀 기록이 없으면 지금처럼 거부한다.
    cleared = plan.model_copy(
        deep=True, update={"deploy": plan.deploy.model_copy(update={"local": Section()})}
    )
    with pytest.raises(DdakToolError) as e:
        select_plan(cleared, RunContext("run-1", targets="both"))
    assert e.value.code is ErrorCode.PLAN_INVALID
    both_empty = plan.model_copy(
        deep=True,
        update={
            "deploy": plan.deploy.model_copy(
                update={"cloud": plan.deploy.local.model_copy(deep=True)}
            )
        },
    )
    with pytest.raises(DdakToolError):
        select_plan(both_empty, RunContext("run-1", targets="both"))


def test_single_target_still_requires_its_section() -> None:
    with pytest.raises(DdakToolError) as e:
        select_plan(_plan(), RunContext("run-1", targets="onprem"))
    assert e.value.code is ErrorCode.PLAN_INVALID
    cloud = select_plan(_plan(), RunContext("run-1", targets="cloud"))
    assert cloud.deploy.cloud.steps and not cloud.deploy.local.steps


class FakeInput(ToolInput):
    target: Target | None = None
    tier: str | None = None
    mode: str | None = None
    scenarios: list[str] | None = None


class FakeOutput(ContractModel):
    passed: bool


def _registry(plan: Plan, calls: list[tuple[str, Any]]) -> Registry:
    sections = (plan.build, plan.deploy.local, plan.deploy.cloud, plan.verify)
    names = sorted({s.tool for sec in sections for s in sec.steps})
    registry = Registry([spec_for(name) for name in names])

    def make(name: str):
        async def fake(inp: FakeInput, ctx: RunContext) -> FakeOutput:
            calls.append((name, inp.target))
            return FakeOutput(passed=True)

        return fake

    for name in names:
        registry.tool(name)(make(name))
    return registry


def test_local_excluded_and_cloud_done_is_succeeded() -> None:
    plan = _plan()
    calls: list[tuple[str, Any]] = []
    ctx = RunContext("run-1", targets="both", lock_token="lock-placeholder")
    result = asyncio.run(Executor(_registry(plan, calls)).run(plan, ctx))
    assert result.status is RunStatus.SUCCEEDED
    assert result.tracks["local"] is TrackStatus.NOT_APPLICABLE
    assert result.tracks["cloud"] is TrackStatus.DONE
    assert ("health_check", Target.CLOUD) in calls
    assert all(target is not Target.LOCAL for _, target in calls)
    assert ("post_report", None) in calls
