"""승인 전에 사용자가 고른 실행 대상을 계획에 반영한다."""

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Plan, Section
from ddak.core.snapshots import digest_json


def select_plan(plan: Plan, ctx: RunContext) -> Plan:
    if ctx.targets is None:
        return plan
    required = (
        ("local", "cloud")
        if ctx.targets == "both"
        else ("local" if ctx.targets == "onprem" else "cloud",)
    )
    if any(not getattr(plan.deploy, target).steps for target in required):
        raise DdakToolError(ErrorCode.PLAN_INVALID, "선택한 대상의 배포 계획이 없다")
    if ctx.targets == "both":
        return plan
    if plan.plan_hash and plan.plan_hash != digest_json(
        plan.model_dump(mode="json", by_alias=True, exclude={"plan_hash"})
    ):
        raise DdakToolError(ErrorCode.PLAN_INVALID, "대상 선택 전 계획 해시가 다르다")
    selected = "local" if ctx.targets == "onprem" else "cloud"
    omitted = "cloud" if selected == "local" else "local"
    keep = [
        s
        for s in plan.verify.steps
        if (s.run == "finally" and (s.target is None or s.target.value == selected))
        or (s.run is None and s.target is not None and s.target.value == selected)
    ]
    return plan.model_copy(
        deep=True,
        update={
            "deploy": plan.deploy.model_copy(deep=True, update={omitted: Section()}),
            "verify": plan.verify.model_copy(deep=True, update={"steps": keep, "signal": None}),
            "plan_hash": None,
        },
    )
