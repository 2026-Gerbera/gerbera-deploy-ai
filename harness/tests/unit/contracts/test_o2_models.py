"""O2 구간 계약 모델 테스트."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ddak.core.contracts import tools as T
from ddak.core.contracts.base import ToolInput
from ddak.core.contracts.deploy_config import DeployConfig, TierConfig
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.plan import Plan
from ddak.core.contracts.plan_draft import PlanDecisions, PlanDraft, StepDecision
from ddak.core.contracts.plan_facts import EnvKey, Facts
from ddak.core.contracts.step_catalog import FORBIDDEN_PARAM_KEYS, catalog_steps

HARNESS = Path(__file__).resolve().parents[3]
H = "sha256:" + "a" * 64


def req(**kw):
    base = {"project": "flaskr", "repo_url": "https://github.com/o/r", "target": "both"}
    return DeployRequest(**{**base, **kw})


@pytest.mark.parametrize(
    "kw",
    [
        {"project": "Bad"},
        {"target": "edge"},
        {"ref": "-x"},
        {"ref": "a..b"},
        {"ref": "a b"},
        {"extra": 1},
        {"repo_url": "https://user:pw@github.com/o/r"},
        {"repo_url": "https://tok@github.com/o/r"},
        {"repo_url": "https://github.com/o/r?token=abc"},
    ],
)
def test_request_rejects(kw):
    with pytest.raises(ValidationError):
        req(**kw)


def test_request_credential_error_hides_value():
    secret = "s3" + "cr3t"
    with pytest.raises(ValidationError) as e:
        req(repo_url=f"https://u:{secret}@github.com/o/r")
    assert secret not in str(e.value)


def test_request_ok():
    r = req(ref="release/v2")
    assert r.mode == "update" and r.code_patch is False


@pytest.mark.parametrize("p", ["/etc", "../x", "a/../b", "C:/x", "a\\b", ""])
def test_config_rejects_paths(p):
    with pytest.raises(ValidationError):
        TierConfig(paths=(p,))
    with pytest.raises(ValidationError):
        TierConfig(dockerfile=p)
    with pytest.raises(ValidationError):
        DeployConfig(tiers={"web": {}}, migrations_dir=p)


def test_config_rejects_tier_name_and_extra():
    with pytest.raises(ValidationError):
        DeployConfig(tiers={"Web": {}})
    with pytest.raises(ValidationError):
        DeployConfig(tiers={"web": {}}, domain="x")
    with pytest.raises(ValidationError):
        DeployConfig(tiers={})


def test_example_yaml_passes():
    cfg = DeployConfig(**yaml.safe_load((HARNESS / "fixtures/deploy.example.yaml").read_text()))
    assert set(cfg.tiers) == {"web", "was"} and cfg.tiers["was"].dockerfile is None


def facts(**kw):
    base = dict(
        project="p", mode="update", target="both", tiers=("web",),
        changed={"local": {"web": True}, "cloud": {"web": True}},
        source_snapshot_hash=H, facts_hash=H,
    )  # fmt: skip
    return Facts(**{**base, **kw})


def test_facts_roundtrip_and_frozen():
    f = facts(env_keys=(EnvKey(name="SECRET_KEY", kind="secret"),))
    assert Facts.model_validate_json(f.model_dump_json()) == f
    with pytest.raises(ValidationError):
        f.project = "x"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        facts(bogus=1)
    with pytest.raises(ValidationError):
        EnvKey(name="lower", kind="plain")


def test_decisions_whitelist():
    PlanDecisions(decisions=(StepDecision(id="build.web", include=False, reason="x"),))
    for bad in (
        {"id": "build.web", "include": True, "reason": "x", "params": {"domain": "a"}},
        {"id": "build.web", "include": True, "reason": "x", "path": "/x"},
        {"id": "BAD", "include": True, "reason": "x"},
    ):
        with pytest.raises(ValidationError):
            PlanDecisions.model_validate({"decisions": [bad]})
    with pytest.raises(ValidationError):
        PlanDecisions.model_validate({"decisions": [], "planner": {}})
    d = PlanDraft.model_validate({"decisions": [], "planner": {"by": "ai", "provider": "x"}})
    assert PlanDraft.model_validate_json(d.model_dump_json()) == d


def _norm(s):
    return (s["tool"], s.get("target"), s.get("tier"), s["layer"], tuple(s.get("wait_for", [])),
            s.get("signal"))  # fmt: skip


def test_catalog_matches_golden():
    g = json.loads((HARNESS / "fixtures/plans/golden_v2_update.json").read_text())
    gold = {}
    for sec in (g["build"], g["deploy"]["local"], g["deploy"]["cloud"], g["verify"]):
        for s in sec["steps"] + sec["skipped"]:
            gold[s["id"]] = s
    cat = {s.id: s for s in catalog_steps(["web", "was"], "both")}
    # 의도된 차이(플랜 충돌 기록): 골든 deploy.app.cloud -> deploy.<tier>.cloud,
    # deploy.storage.*는 골든에 없음
    gold.pop("deploy.app.cloud")

    assert set(gold) <= set(cat)
    assert set(cat) - set(gold) == {
        "deploy.storage.local",
        "deploy.storage.cloud",
        "deploy.dbinit.local",
        "deploy.web.cloud",
        "deploy.was.cloud",
    }
    for sid, s in gold.items():
        actual = cat[sid].model_dump(mode="json")
        if "skip_rule" in s:
            actual["wait_for"] = []
        assert _norm(actual) == _norm(s), sid
    for t in ("web", "was"):
        c = cat[f"deploy.{t}.cloud"]
        assert c.wait_for == ("images_ready",) and c.tier == t


def test_catalog_order_and_single_target():
    ids = [s.id for s in catalog_steps(["web", "was"], "both")]
    assert ids.index("build.was") < ids.index("deploy.config.local") < ids.index("deploy.tls.cloud")
    assert (
        ids.index("deploy.tls.cloud") < ids.index("deploy.db.cloud") < ids.index("verify.compare")
    )
    assert ids[-3:] == ["verify.compare", "verify.report", "verify.watch.cloud"]
    local = catalog_steps(["web"], "local")
    assert not any(s.id.endswith(".cloud") for s in local)
    assert local[-2].wait_for == ("local_verified",)
    for s in catalog_steps(["web"], "cloud"):  # 환경 간 대기 없음
        if s.effect == "state_change":
            assert "local_verified" not in s.wait_for, s.id


def test_forbidden_keys_not_allowed_params():
    for s in catalog_steps(["web"], "both"):
        assert not set(s.allowed_params) & FORBIDDEN_PARAM_KEYS
    assert {
        "domain",
        "host",
        "url",
        "zone",
        "image",
        "digest",
        "arn",
        "command",
    } <= FORBIDDEN_PARAM_KEYS


@pytest.mark.parametrize(
    "name",
    [
        "ReceiveDeployRequest",
        "DetectChangedTiers",
        "AnalyzeProject",
        "GeneratePlan",
        "ValidatePlan",
    ],
)
def test_tool_models(name):
    inp, out = getattr(T, f"{name}Input"), getattr(T, f"{name}Output")
    assert issubclass(inp, ToolInput)
    inp.model_json_schema()
    out.model_json_schema()
    if name == "ValidatePlan":
        assert out is Plan


def test_invalidated_extended_values() -> None:
    from ddak.core.contracts.plan import Invalidated

    old = Invalidated(id="verify.report", why="x")
    assert (old.attempt, old.result) == ("skip", "forced_include")
    new = Invalidated(id="build.web", attempt="include", result="forced_skip", why="tree_unchanged")
    assert Invalidated.model_validate_json(new.model_dump_json()) == new
    with pytest.raises(ValidationError):
        Invalidated(id="a.b", attempt="drop", why="x")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Invalidated(id="a.b", result="forced_x", why="x")  # type: ignore[arg-type]


def test_golden_plan_parses_unchanged() -> None:
    gold = json.loads((HARNESS / "fixtures/plans/golden_v2_update.json").read_text())
    p = Plan.model_validate(gold)
    assert all(i.attempt == "skip" and i.result == "forced_include" for i in p.invalidated)
