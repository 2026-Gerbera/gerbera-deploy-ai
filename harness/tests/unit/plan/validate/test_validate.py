"""plan/validate: 조립·규칙·토글. 골든 플랜과 step 집합 비교는 test_golden_step_set."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ddak.app import load_tools
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import By, RunMode
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Plan, Planner
from ddak.core.contracts.plan_draft import PlanDraft, StepDecision
from ddak.core.contracts.plan_facts import EnvKey, Facts
from ddak.core.contracts.tools.validate_plan import ValidatePlanInput
from ddak.core.registry import spec_for
from ddak.plan.validate import validate_plan
from ddak.plan.validate.rules import check_draft, check_params

H = "sha256:" + "a" * 64
GOLDEN = Path(__file__).parents[4] / "fixtures" / "plans" / "golden_v2_update.json"


def facts(**kw: Any) -> Facts:
    base: dict[str, Any] = dict(
        project="flaskr", mode=RunMode.UPDATE, target="both", tiers=("was", "web"),
        changed={"local": {"was": True, "web": False}, "cloud": {"was": True, "web": False}},
        new_migrations=("0002",),
        env_keys=(
            EnvKey(name="SECRET_KEY", kind="secret", tier="was"),
            EnvKey(name="SESSION_COOKIE_SECURE", kind="plain", tier="was"),
        ),
        db_initialized={"local": True, "cloud": True}, infra_inputs_changed=True,
        source_snapshot_hash=H, facts_hash=H,
    )  # fmt: skip
    return Facts(**{**base, **kw})


def draft(*pairs: tuple[str, bool]) -> PlanDraft:
    return PlanDraft(
        decisions=tuple(StepDecision(id=i, include=b, reason="t") for i, b in pairs),
        planner=Planner(by=By.AI, provider="replay"),
    )


def run(f: Facts, d: PlanDraft | None = None, **toggles: bool) -> Plan:
    ctx = RunContext("run-1", toggles={"code_patch": False, **toggles})
    return validate_plan(ValidatePlanInput(run_id="run-1", facts=f, draft=d), ctx)


def ids(plan: Plan) -> set[str]:
    secs = [plan.build, plan.deploy.local, plan.deploy.cloud, plan.verify]
    return {s.id for sec in secs for s in sec.steps}


def test_rule_plan_validates_and_marks_fallback() -> None:
    p = run(facts())
    Plan.model_validate(p.model_dump(by_alias=True))
    assert p.planner and p.planner.by is By.RULE and p.planner.fallback
    assert p.facts_hash == H and p.plan_hash is None


def test_golden_step_set() -> None:
    gold = json.loads(GOLDEN.read_text())
    key = lambda s: (  # noqa: E731
        s["tool"], s.get("target"), s.get("tier"), s["layer"],
        tuple(s.get("wait_for", [])), s.get("signal"),
    )  # fmt: skip
    g = {
        key(s)
        for sec in (gold["build"], gold["deploy"]["local"], gold["deploy"]["cloud"], gold["verify"])
        for s in sec["steps"]
    }  # fmt: skip
    p = run(facts()).model_dump(mode="json")
    mine = {
        key(s) for sec in (p["build"], p["deploy"]["local"], p["deploy"]["cloud"], p["verify"])
        for s in sec["steps"]
    }  # fmt: skip
    # 알려진 차이: 골든 deploy.app.cloud(tier=None) 대신 deploy.<tier>.cloud(tier=was),
    # (tool, target, tier, layer, wait_for, signal)에 id는 없으므로 이 차이만 남는다.
    only_gold, only_mine = g - mine, mine - g
    assert {k[0] for k in only_gold} == {"deploy_tier"} and len(only_gold) == 1
    assert {k[0] for k in only_mine} == {"deploy_tier"} and len(only_mine) == 1
    assert (g - only_gold) == (mine - only_mine)


def test_deterministic_and_order_independent() -> None:
    a = draft(("verify.watch.cloud", True), ("deploy.storage.local", False))
    b = draft(("deploy.storage.local", False), ("verify.watch.cloud", True))
    pa, pb = run(facts(), a), run(facts(), b)
    assert run(facts(), a).model_dump() == pa.model_dump()
    assert pa.model_dump() == pb.model_dump()


def test_rejects_unknown_id() -> None:
    with pytest.raises(DdakToolError) as e:
        run(facts(), draft(("deploy.nope.cloud", True)))
    assert e.value.code is ErrorCode.PLAN_INVALID


@pytest.mark.parametrize("key", ["domain", "digest", "command"])
def test_forbidden_params_always_rejected(key: str) -> None:
    for strict in (True, False):
        with pytest.raises(DdakToolError) as e:
            check_params("deploy.db.cloud", {key: "x"}, ("migrations",), strict=strict)
        assert e.value.code is ErrorCode.PLAN_INVALID


def test_disallowed_param_strict_vs_relaxed() -> None:
    with pytest.raises(DdakToolError):
        check_params("s.x", {"foo": 1, "migrations": []}, ("migrations",), strict=True)
    clean, w = check_params("s.x", {"foo": 1, "migrations": []}, ("migrations",), strict=False)
    assert clean == {"migrations": []} and w[0].code == "ai_draft_item_dropped"


def test_mandatory_forced_include_recorded() -> None:
    p = run(facts(), draft(("verify.health.local", False), ("verify.report", False)))
    assert {"verify.health.local", "verify.report"} <= ids(p)
    assert {i.id for i in p.invalidated} == {"verify.health.local", "verify.report"}
    assert all(i.result == "forced_include" for i in p.invalidated)


def test_conditional_ai_skip_is_overridden() -> None:
    p = run(facts(), draft(("deploy.db.cloud", False)))
    assert "deploy.db.cloud" in ids(p)
    assert [i.id for i in p.invalidated] == ["deploy.db.cloud"]


def test_skip_rules() -> None:
    p = run(facts(new_migrations=(), env_keys=(), infra_inputs_changed=False))
    rules = {
        s.id: s.skip_rule
        for sec in (p.build, p.deploy.local, p.deploy.cloud, p.verify)
        for s in sec.skipped
    }
    assert rules["build.web"] == "tree_unchanged"
    assert rules["deploy.web.cloud"] == "digest_deployed"
    assert rules["deploy.db.local"] == "no_new_migrations"
    assert rules["deploy.dbinit.cloud"] == "db_initialized"
    assert rules["deploy.config.cloud"] == "no_new_keys"
    assert rules["deploy.secrets.cloud"] == "no_new_secret"
    assert rules["deploy.infra.cloud"] == "no_infra_change"
    assert rules["verify.watch.cloud"] == "optional"


def test_params_filled_and_secret_keys_only() -> None:
    p = run(facts())
    cloud = {s.id: s for s in p.deploy.cloud.steps}
    assert cloud["deploy.db.cloud"].params == {"migrations": ["0002"]}
    assert cloud["deploy.secrets.cloud"].params == {"keys": ["SECRET_KEY"]}
    assert cloud["deploy.config.cloud"].params["keys"] == ["SECRET_KEY", "SESSION_COOKIE_SECURE"]


def test_optional_follows_ai() -> None:
    p = run(facts(), draft(("verify.watch.cloud", True)))
    assert "verify.watch.cloud" in ids(p)
    assert [s.id for s in run(facts()).verify.skipped] == ["verify.watch.cloud"]


def test_gate_local_verified() -> None:
    both = {s.id: s for s in run(facts()).deploy.cloud.steps}
    assert "local_verified" in both["deploy.db.cloud"].wait_for
    only_cloud = run(facts(target="cloud", changed={"cloud": {"was": True, "web": False}}))
    assert all("local_verified" not in s.wait_for for s in only_cloud.deploy.cloud.steps)
    assert not only_cloud.deploy.local.steps


def test_toggle_off_relaxes_but_keeps_floor() -> None:
    d = draft(("deploy.nope.cloud", True), ("verify.report", False), ("deploy.db.cloud", False))
    with pytest.raises(DdakToolError):
        run(facts(), d)
    p = run(facts(), d, validate_ai_draft=False)
    codes = [w.code for w in p.warnings]
    assert "ai_draft_item_dropped" in codes and "ai_check_disabled" in codes
    assert p.toggles["validate_ai_draft"] is False
    assert "verify.report" in ids(p) and "verify.report" in {i.id for i in p.invalidated}
    assert "deploy.db.cloud" in ids(p) and "deploy.db.cloud" not in {i.id for i in p.invalidated}
    on = run(facts(), draft(("deploy.db.cloud", False)))
    assert "ai_check_disabled" not in [w.code for w in on.warnings]
    assert on.toggles == {"code_patch": False}


def test_check_draft_relaxed_drops_unknown() -> None:
    got, w = check_draft(draft(("x.y", True), ("a.b", True)).decisions, {"a.b"}, strict=False)
    assert list(got) == ["a.b"] and len(w) == 1


def test_registered_and_not_ai() -> None:
    assert "validate_plan" in load_tools().registered()
    assert spec_for("validate_plan").uses_ai is False
