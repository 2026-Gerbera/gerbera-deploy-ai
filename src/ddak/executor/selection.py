"""승인 전에 사용자가 고른 실행 대상을 계획에 반영한다."""

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Plan, Section
from ddak.core.snapshots import digest_json

# plan/validate가 바뀐 것이 없는 환경을 통째로 뺄 때 남기는 skip_rule
_ENV_UNCHANGED = "env_unchanged"


def _unchanged(section: Section) -> bool:
    return not section.steps and any(s.skip_rule == _ENV_UNCHANGED for s in section.skipped)


def select_plan(plan: Plan, ctx: RunContext) -> Plan:
    if ctx.targets is None:
        return plan
    required = (
        ("local", "cloud")
        if ctx.targets == "both"
        else ("local" if ctx.targets == "onprem" else "cloud",)
    )
    sections = [getattr(plan.deploy, target) for target in required]
    # both에서 빈 섹션은 변경 없는 환경을 뺀 경우만 허용한다. 남는 환경은 하나 이상이어야 한다.
    if any(
        not s.steps and not (ctx.targets == "both" and _unchanged(s)) for s in sections
    ) or not any(s.steps for s in sections):
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
