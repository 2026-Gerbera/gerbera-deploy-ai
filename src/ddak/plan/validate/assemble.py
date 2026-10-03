"""카탈로그 기준 조립: 후보 step -> 결정 -> 섹션. 같은 입력이면 항상 같은 Plan."""

from __future__ import annotations

from collections.abc import Collection

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import By, Layer
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import (
    Invalidated,
    Plan,
    Planner,
    PlanStep,
    PlanWarning,
    Section,
    SkippedStep,
)
from ddak.core.contracts.step_catalog import StepDef, catalog_steps
from ddak.core.contracts.tools.validate_plan import ValidatePlanInput
from ddak.core.registry import REGISTRY
from ddak.core.smoke import SMOKE_GROUPS
from ddak.plan.validate import rules as R

TOGGLE = "strict_ai_check"  # 없으면 False = 기본 검사


def assemble(
    inp: ValidatePlanInput, ctx: RunContext, *, registered_tools: Collection[str] | None = None
) -> Plan:
    # 앱의 load_tools 이후 호출 시점에 조회한다. import 시점의 빈 레지스트리를 고정하지 않는다.
    registered = set(REGISTRY.registered() if registered_tools is None else registered_tools)
    facts = inp.facts
    strict = ctx.toggles.get(TOGGLE, False)
    catalog = catalog_steps(facts.tiers, facts.target)
    by_id = {s.id: s for s in catalog}
    warns: list[PlanWarning] = []
    ai: dict[str, bool] = {}
    if inp.draft is not None:
        decided, w = R.check_draft(inp.draft.decisions, by_id, strict=strict)
        warns += w
        ai = {i: d.include for i, d in decided.items()}
    warns += R.check_modified_migrations(facts, strict=strict)
    groups = ["base"]
    for group in facts.smoke_groups:
        if group not in SMOKE_GROUPS:
            warns.append(R.warning("unknown_smoke_group", "허용되지 않은 스모크 그룹을 버렸다"))
        elif group not in groups:
            groups.append(group)

    # 1) 결정
    chosen: dict[
        str, tuple[bool, dict, str, By, str | None]
    ] = {}  # id -> inc, params, why, by, skip_rule
    invalid: list[Invalidated] = []
    for s in catalog:
        ruled = R.rule_eval(s, facts) if s.layer is Layer.CONDITIONAL else None
        params = {**s.default_params, **(ruled.params if ruled else {})}
        if s.tool == "smoke_test":
            params["scenarios"] = groups.copy()
        want = ai.get(s.id)
        if s.layer is Layer.MANDATORY:
            inc, why, by, rid = True, "필수 step", By.RULE, None
            if want is False:
                invalid.append(
                    Invalidated(id=s.id, why=R.clip(f"{R.R_MANDATORY}: 필수 step은 뺄 수 없다"))
                )
        elif ruled is not None:
            inc, why, by, rid = ruled.include, ruled.reason, By.RULE, s.skip_rule
            if inc and want is False:
                invalid.append(Invalidated(id=s.id, why=R.clip(f"{s.skip_rule}: {ruled.reason}")))
            elif not inc and want is True:
                invalid.append(
                    Invalidated(
                        id=s.id,
                        attempt="include",
                        result="forced_skip",
                        why=R.clip(f"{s.skip_rule}: {ruled.reason}"),
                    )
                )
        else:  # OPTIONAL: AI 결정, 없으면 기본 제외
            inc = bool(want)
            why = (decided[s.id].reason or "AI 결정") if want is not None else "기본 제외"
            by, rid = (By.AI if want is not None else By.RULE), "optional"
            if s.tool not in registered:
                inc, by, why = False, By.RULE, f"미등록 선택 툴: {s.tool} ({s.id})"
                warns.append(R.warning("unregistered_optional_tool", why))
                if want is True:
                    invalid.append(
                        Invalidated(id=s.id, attempt="include", result="forced_skip", why=why)
                    )
        chosen[s.id] = (inc, params, why, by, rid)

    # 2) R-couple
    if chosen.get("deploy.secrets.cloud", (False,))[0]:
        for cid in R.couple_targets(facts, facts.tiers):
            if cid in chosen and not chosen[cid][0]:
                s = by_id[cid]
                ruled = R.rule_eval(s, facts) if s.layer is Layer.CONDITIONAL else None
                params = {**s.default_params, **(ruled.params if ruled else {})}
                chosen[cid] = (
                    True,
                    params,
                    f"{R.R_COUPLE}: secrets와 함께 포함",
                    By.RULE,
                    s.skip_rule,
                )
                invalid.append(
                    Invalidated(
                        id=cid,
                        by=By.AI if ai.get(cid) is False else By.RULE,
                        why=R.clip(f"{R.R_COUPLE}: deploy.secrets.cloud와 함께 포함해야 한다"),
                    )
                )

    # R-couple이 다시 포함시킨 step은 강제 제외 기록이 틀리므로 지운다
    invalid = [i for i in invalid if not (i.result == "forced_skip" and chosen[i.id][0])]

    # 3) env_unchanged: 상태를 바꾸는 step이 없는 환경은 헬스·스모크만 남기지 않고 통째로 뺀다.
    envs = ["local", "cloud"] if facts.target == "both" else [facts.target]
    idle = R.unchanged_envs(facts, catalog, {sid: c[0] for sid, c in chosen.items()})
    if len(idle) == len(envs):
        # PLAN_INVALID가 아니다: AI 재지시로 고칠 수 있는 계획 오류가 아니다.
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "배포할 변경 없음")
    if idle:
        dropped = {s.id for s in catalog if s.section in {f"deploy.{e}" for e in idle}}
        dropped.add("verify.compare")  # 남은 활성 환경이 하나뿐이다
        if "cloud" in idle:
            dropped.add("verify.watch.cloud")
        moved = {sid for sid in dropped if sid in chosen and chosen[sid][0]}
        for sid in moved:
            params = chosen[sid][1]
            chosen[sid] = (False, params, R.ENV_UNCHANGED_REASON, By.RULE, R.R_ENV_UNCHANGED)
        invalid = [i for i in invalid if not (i.result == "forced_include" and i.id in moved)]
        for env in idle:
            label = {"local": "온프레미스", "cloud": "클라우드"}[env]
            warns.append(R.warning(R.W_ENV_UNCHANGED, f"{label}: {R.ENV_UNCHANGED_REASON}"))
        envs = [e for e in envs if e not in idle]

    # 4) 조립: 신호 의존은 카탈로그 값을 그대로 사용한다.
    sections = {k: Section() for k in ("build", "deploy.local", "deploy.cloud", "verify")}
    sections["build"] = Section(signal="images_ready")
    for s in catalog:
        inc, params, why, by, rid = chosen[s.id]
        sec = sections[s.section]
        if inc:
            params, w = R.check_params(s.id, params, s.allowed_params, strict=False)
            warns += w
            sec.steps.append(_step(s, params, why, by))
        else:
            sec.skipped.append(
                SkippedStep(
                    id=s.id, tool=s.tool, target=s.target, tier=s.tier, layer=s.layer,
                    by=by, reason=R.clip(why), skip_rule=rid,
                )
            )  # fmt: skip

    R.check_migrations(facts, {e: sections[f"deploy.{e}"].steps for e in envs})

    return Plan(
        run_id=inp.run_id,
        project=facts.project,
        mode=facts.mode,
        facts_hash=facts.facts_hash,
        planner=inp.draft.planner
        if inp.draft
        else Planner(by=By.RULE, provider="rule", fallback=True),
        toggles=dict(ctx.toggles),
        build=sections["build"],
        deploy={"local": sections["deploy.local"], "cloud": sections["deploy.cloud"]},  # type: ignore[arg-type]
        verify=sections["verify"],
        invalidated=invalid,
        warnings=warns,
    )


def _step(s: StepDef, params: dict, why: str, by: By) -> PlanStep:
    return PlanStep(
        id=s.id, tool=s.tool, target=s.target, tier=s.tier, params=params,
        layer=s.layer, effect=s.effect, by=by, reason=R.clip(why), evidence=R.evidence(s),
        wait_for=list(s.wait_for),
        signal=s.signal, run=s.run,
    )  # fmt: skip
