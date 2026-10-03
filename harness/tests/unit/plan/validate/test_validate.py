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


def test_strict_rejects_unknown_id() -> None:
    with pytest.raises(DdakToolError) as e:
        run(facts(), draft(("deploy.nope.cloud", True)), strict_ai_check=True)
    assert e.value.code is ErrorCode.PLAN_INVALID


@pytest.mark.parametrize("key", ["domain", "digest", "command"])
def test_forbidden_params_always_rejected(key: str) -> None:
    for strict in (True, False):
        with pytest.raises(DdakToolError) as e:
            check_params("deploy.migrate.cloud", {key: "x"}, ("migrations",), strict=strict)
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
    p = run(facts(), draft(("deploy.migrate.cloud", False)))
    assert "deploy.migrate.cloud" in ids(p)
    assert [i.id for i in p.invalidated] == ["deploy.migrate.cloud"]


def test_skip_rules() -> None:
    p = run(facts(new_migrations=(), env_keys=(), infra_inputs_changed=False))
    rules = {
        s.id: s.skip_rule
        for sec in (p.build, p.deploy.local, p.deploy.cloud, p.verify)
        for s in sec.skipped
    }
    assert rules["build.web"] == "tree_unchanged"
    assert rules["deploy.web.cloud"] == "digest_deployed"
    assert rules["deploy.migrate.local"] == "no_new_migrations"
    assert rules["deploy.dbinit.cloud"] == "db_initialized"
    assert rules["deploy.config.cloud"] == "no_new_keys"
    assert rules["deploy.secrets.cloud"] == "no_new_secret"
    assert rules["deploy.infra.cloud"] == "no_infra_change"
    assert rules["verify.watch.cloud"] == "optional"


def test_params_filled_and_secret_keys_only() -> None:
    p = run(facts())
    cloud = {s.id: s for s in p.deploy.cloud.steps}
    assert cloud["deploy.migrate.cloud"].params == {"migrations": ["0002"]}
    assert cloud["deploy.secrets.cloud"].params == {"keys": ["SECRET_KEY"]}
    assert cloud["deploy.config.cloud"].params["keys"] == ["SECRET_KEY", "SESSION_COOKIE_SECURE"]


def test_optional_follows_ai_when_registered() -> None:
    p = validate_plan(
        ValidatePlanInput(run_id="run-1", facts=facts(), draft=draft(("verify.watch.cloud", True))),
        RunContext("run-1"),
        registered_tools={"watch_post_deploy"},
    )
    assert "verify.watch.cloud" in ids(p)
    assert [s.id for s in run(facts()).verify.skipped] == ["verify.watch.cloud"]


def test_no_inter_environment_wait() -> None:
    both = {s.id: s for s in run(facts()).deploy.cloud.steps}
    assert both["deploy.migrate.cloud"].wait_for == ["images_ready"]
    only_cloud = run(facts(target="cloud", changed={"cloud": {"was": True, "web": False}}))
    assert all("local_verified" not in s.wait_for for s in only_cloud.deploy.cloud.steps)
    assert not only_cloud.deploy.local.steps


def test_default_mode_drops_strict_mode_rejects() -> None:
    unknown = draft(("deploy.nope.cloud", True))
    p = run(facts(), unknown)  # 기본 검사: 버림 + 경고
    assert "ai_draft_item_dropped" in [w.code for w in p.warnings]
    assert p.toggles == {"code_patch": False}
    with pytest.raises(DdakToolError) as e:
        run(facts(), unknown, strict_ai_check=True)
    assert e.value.code is ErrorCode.PLAN_INVALID


def test_both_modes_keep_floor() -> None:
    d = draft(("verify.report", False), ("deploy.migrate.cloud", False))
    for strict in (False, True):
        p = run(facts(), d, strict_ai_check=strict)
        assert p.toggles["strict_ai_check"] is strict
        assert {"verify.report", "deploy.migrate.cloud"} <= ids(p)
        assert {"verify.report", "deploy.migrate.cloud"} == {i.id for i in p.invalidated}
        assert all(i.result == "forced_include" for i in p.invalidated)


def test_forbidden_param_rejected_in_both_modes() -> None:
    for strict in (False, True):
        with pytest.raises(DdakToolError):
            check_params("deploy.migrate.cloud", {"domain": "x"}, ("migrations",), strict=strict)


def test_forced_skip_recorded_both_modes() -> None:
    f = facts(changed={"local": {"was": True, "web": False}, "cloud": {"was": True, "web": False}})
    for strict in (False, True):
        p = run(f, draft(("build.web", True), ("build.was", True)), strict_ai_check=strict)
        rec = [i for i in p.invalidated if i.attempt == "include"]
        assert [(i.id, i.result, i.by) for i in rec] == [("build.web", "forced_skip", By.AI)]
        assert rec[0].why.startswith("tree_unchanged")
        assert "build.web" not in ids(p)
    assert not run(f, draft(("build.web", False))).invalidated  # 규칙과 일치


def _env_ids(p: Plan, env: str) -> list[str]:
    return [s.id for s in getattr(p.deploy, env).steps]


@pytest.mark.parametrize("target", ["local", "cloud", "both"])
def test_r_migration_present(target: str) -> None:
    ch = {e: {"was": True, "web": True} for e in ("local", "cloud") if target in (e, "both")}
    p = run(facts(target=target, changed=ch, new_migrations=("0003", "0002")))
    envs = ["local", "cloud"] if target == "both" else [target]
    for e in envs:
        order = _env_ids(p, e)
        assert order.index(f"deploy.migrate.{e}") < order.index(f"deploy.was.{e}")
        db = next(s for s in getattr(p.deploy, e).steps if s.id == f"deploy.migrate.{e}")
        assert db.params == {"migrations": ["0002", "0003"]}


@pytest.mark.parametrize("target", ["local", "cloud", "both"])
def test_r_migration_absent(target: str) -> None:
    p = run(facts(target=target, new_migrations=()))
    assert not [
        i for e in ("local", "cloud") for i in _env_ids(p, e) if i.startswith("deploy.migrate.")
    ]


def test_r_migration_violations_rejected() -> None:
    from ddak.core.contracts.enums import Effect, Layer
    from ddak.core.contracts.plan import PlanStep
    from ddak.plan.validate.rules import check_migrations

    def st(i: str, tool: str, **params: Any) -> PlanStep:
        return PlanStep(
            id=i, tool=tool, layer=Layer.CONDITIONAL, effect=Effect.STATE_CHANGE, params=params
        )

    db = st("deploy.migrate.cloud", "prepare_db", migrations=["0002"])
    tier = st("deploy.was.cloud", "deploy_tier")
    f = facts(target="cloud")
    check_migrations(f, {"cloud": [db, tier]})
    for steps in (
        [tier],
        [tier, db],
        [st("deploy.migrate.cloud", "prepare_db", migrations=[]), tier],
    ):
        with pytest.raises(DdakToolError) as e:
            check_migrations(f, {"cloud": steps})
        assert e.value.code is ErrorCode.PLAN_INVALID and "R-migration" in e.value.message
    with pytest.raises(DdakToolError):
        check_migrations(facts(target="cloud", new_migrations=()), {"cloud": [db, tier]})


def test_modified_migration_warns_strict_rejects() -> None:
    f = facts(modified_migrations=("0001",))
    assert "migration_modified" in [w.code for w in run(f).warnings]
    with pytest.raises(DdakToolError) as e:
        run(f, strict_ai_check=True)
    assert e.value.code is ErrorCode.PLAN_INVALID


def test_check_draft_relaxed_drops_unknown() -> None:
    got, w = check_draft(draft(("x.y", True), ("a.b", True)).decisions, {"a.b"}, strict=False)
    assert list(got) == ["a.b"] and len(w) == 1


def test_registered_and_not_ai() -> None:
    assert "validate_plan" in load_tools().registered()
    assert spec_for("validate_plan").uses_ai is False
