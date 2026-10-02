"""계획 검증 규칙(순수 함수). 규칙 id는 skipped.skip_rule · invalidated.why 에 쓴다."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import PlanStep, PlanWarning
from ddak.core.contracts.plan_draft import StepDecision
from ddak.core.contracts.plan_facts import EnvKey, Facts
from ddak.core.contracts.step_catalog import FORBIDDEN_PARAM_KEYS, StepDef

R_IDS = "R-ids"
R_PARAMS = "R-params"
R_MANDATORY = "R-mandatory"
R_COUPLE = "R-couple"
R_GATE = "R-gate"
R_FACTS = "R-facts"
R_MIGRATION = "R-migration"

W_ITEM_DROPPED = "ai_draft_item_dropped"
W_MIGRATION_MODIFIED = "migration_modified"


def clip(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _invalid(msg: str) -> DdakToolError:
    return DdakToolError(ErrorCode.PLAN_INVALID, clip(msg))


def warning(code: str, message: str) -> PlanWarning:
    return PlanWarning(code=code, message=clip(message))


def check_migrations(facts: Facts, steps: Mapping[str, Iterable[PlanStep]]) -> None:
    """R-migration(끌 수 없음). steps = 환경 -> 그 환경 deploy 섹션의 포함 step(실행 순서).

    새 마이그레이션이 있으면 deploy.migrate.<e>가 정확한 params로 tier 배포보다 앞에 있어야 하고,
    없으면 없어야 한다.
    """
    want = sorted(facts.new_migrations)
    for env, seq in steps.items():
        order = [s.id for s in seq]
        db_id = f"deploy.migrate.{env}"
        if not want:
            if db_id in order:
                raise _invalid(f"{R_MIGRATION}: 새 마이그레이션이 없는데 {db_id}가 있다")
            continue
        if db_id not in order:
            raise _invalid(f"{R_MIGRATION}: 새 마이그레이션 {want}이 있는데 {db_id}가 없다")
        db = next(s for s in seq if s.id == db_id)
        if db.params.get("migrations") != want:
            raise _invalid(f"{R_MIGRATION}: {db_id}의 migrations가 {want}와 다르다")
        pos = order.index(db_id)
        if any(s.tool == "deploy_tier" and order.index(s.id) < pos for s in seq):
            raise _invalid(f"{R_MIGRATION}: {db_id}가 {env} tier 배포보다 앞이어야 한다")


def check_modified_migrations(facts: Facts, *, strict: bool) -> list[PlanWarning]:
    """이미 적용된 마이그레이션 파일이 바뀌었다: 경고, 엄격 모드는 거부."""
    if not facts.modified_migrations:
        return []
    msg = f"적용된 마이그레이션 파일이 바뀌었다: {', '.join(facts.modified_migrations)}"
    if strict:
        raise _invalid(f"{R_MIGRATION}: {msg}")
    return [warning(W_MIGRATION_MODIFIED, msg)]


def check_draft(
    decisions: Iterable[StepDecision], known_ids: Collection[str], *, strict: bool
) -> tuple[dict[str, StepDecision], list[PlanWarning]]:
    """R-ids. strict면 미지 id·충돌하는 중복을 거부, 아니면 그 결정만 버리고 경고한다.

    결과는 초안 순서와 무관하다(중복 충돌은 include=False가 이긴다).
    """
    out: dict[str, StepDecision] = {}
    warns: list[PlanWarning] = []
    for d in decisions:
        if d.id not in known_ids:
            if strict:
                raise _invalid(f"{R_IDS}: 카탈로그에 없는 step id {d.id}")
            warns.append(warning(W_ITEM_DROPPED, f"{R_IDS}: 미지 step id {d.id} 결정을 버렸다"))
            continue
        prev = out.get(d.id)
        if prev is not None and prev.include != d.include:
            if strict:
                raise _invalid(f"{R_IDS}: step {d.id} 결정이 서로 충돌한다")
            warns.append(warning(W_ITEM_DROPPED, f"{R_IDS}: step {d.id} 충돌 결정은 제외로 처리"))
            if prev.include:
                out[d.id] = d
            continue
        out[d.id] = d
    return out, warns


def check_params(
    step_id: str, params: Mapping[str, Any], allowed: Collection[str], *, strict: bool
) -> tuple[dict[str, Any], list[PlanWarning]]:
    """R-params(V3). 금지 키는 항상 거부. 허용 밖 키는 strict면 거부, 아니면 버림."""
    bad = sorted(FORBIDDEN_PARAM_KEYS.intersection(params))
    if bad:
        raise _invalid(f"{R_PARAMS}: step {step_id} 금지 파라미터 키 {bad}")
    clean: dict[str, Any] = {}
    warns: list[PlanWarning] = []
    for k in sorted(params):
        if k in allowed:
            clean[k] = params[k]
        elif strict:
            raise _invalid(f"{R_PARAMS}: step {step_id} 허용되지 않은 파라미터 키 {k}")
        else:
            warns.append(warning(W_ITEM_DROPPED, f"{R_PARAMS}: {step_id}의 파라미터 {k}를 버렸다"))
    return clean, warns


@dataclass(frozen=True)
class RuleResult:
    include: bool
    params: dict[str, Any]
    reason: str


def _envs(facts: Facts) -> list[str]:
    return ["local", "cloud"] if facts.target == "both" else [facts.target]


def _changed(facts: Facts, env: str, tier: str | None) -> bool:
    return facts.changed.get(env, {}).get(tier or "", True)  # type: ignore[call-overload]


def _tier_keys(facts: Facts, tier: str | None) -> list[EnvKey]:
    return [k for k in facts.env_keys if k.is_new and (k.tier is None or k.tier == tier)]


def new_keys(facts: Facts, *, secret_only: bool = False) -> list[str]:
    return sorted(
        k.name for k in facts.env_keys if k.is_new and (k.kind == "secret" or not secret_only)
    )


def rule_eval(step: StepDef, facts: Facts) -> RuleResult:
    """조건부 step의 결정적 규칙. skip_rule 이름은 step_catalog가 정한다."""
    env = step.target.value if step.target else ""
    rule = step.skip_rule
    params: dict[str, Any] = {}
    if rule == "tree_unchanged":
        inc = any(_changed(facts, e, step.tier) for e in _envs(facts))
        why = "입력 트리 변경" if inc else "입력 트리 변경 없음"
    elif rule == "digest_deployed":
        inc = _changed(facts, env, step.tier) or bool(_tier_keys(facts, step.tier))
        why = "변경 또는 새 env 키 있음" if inc else "배포된 digest와 같고 새 env 키 없음"
    elif rule == "no_new_migrations":
        inc = bool(facts.new_migrations)
        if inc:
            params["migrations"] = sorted(facts.new_migrations)
        why = "새 마이그레이션 있음" if inc else "새 마이그레이션 없음"
    elif rule == "db_initialized":
        inc = not facts.db_initialized.get(env, False)
        why = "앱 DB 초기화 필요" if inc else "앱 DB·계정이 이미 있음"
    elif rule == "no_new_keys":
        names = new_keys(facts)
        inc = bool(names)
        if inc:
            params["keys"] = names
        why = "새 env 키 있음" if inc else "새 env 키 없음"
    elif rule == "no_new_secret":
        names = new_keys(facts, secret_only=True)
        inc = bool(names)
        if inc:
            params["keys"] = names
        why = "새 secret 키 있음" if inc else "새 secret 키 없음"
    elif rule == "no_infra_change":
        inc = facts.infra_inputs_changed
        why = "인프라 입력 변경" if inc else "인프라 입력 변경 없음"
    else:
        raise _invalid(f"조건부 step {step.id}의 규칙 {rule}을 모른다")
    return RuleResult(inc, params, why)


def couple_targets(facts: Facts, tiers: Iterable[str]) -> list[str]:
    """R-couple(V6): secrets.cloud 포함 시 같이 포함해야 하는 step id."""
    secret_tiers = {k.tier for k in facts.env_keys if k.is_new and k.kind == "secret"}
    hit = set(tiers) if None in secret_tiers else {t for t in tiers if t in secret_tiers}
    return ["deploy.config.cloud", *(f"deploy.{t}.cloud" for t in tiers if t in hit)]
