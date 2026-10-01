"""골든 수락: v1 성공 기록 -> v2 커밋(was 변경, 0002, 새 키 2개) -> 골든 plan과 비교."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ddak.core.contracts.plan_facts import FileMeta
from ddak.core.snapshots import file_manifest
from ddak.executor.engine import check_signals
from ddak.plan import plan_deployment
from ddak.plan.detect import facts_reader
from tests.unit.plan.test_flow import SETTINGS, V1, V2, FakeJev, commit, policy, request

GOLDEN = Path(__file__).parents[3] / "fixtures" / "plans" / "golden_v2_update.json"


def _keys(plan: dict[str, Any]) -> set[tuple[Any, ...]]:
    secs = (plan["build"], plan["deploy"]["local"], plan["deploy"]["cloud"], plan["verify"])
    return {
        (s["tool"], s.get("target"), s.get("tier"), s["layer"],
         tuple(s.get("wait_for", [])), s.get("signal"))
        for sec in secs for s in sec["steps"]
    }  # fmt: skip


def test_golden_v2_update(tmp_path: Path) -> None:
    repo = tmp_path / "remote"
    commit(repo, V1)
    pol = policy(tmp_path)
    kw: dict[str, Any] = dict(settings=SETTINGS, fetch_policy=pol, jev_client=FakeJev(0.1))
    v1 = plan_deployment(
        request(repo, code_patch=True, mode="bootstrap"),  # type: ignore[arg-type]
        run_id="run-v1", previous_manifests=lambda p: {"local": None, "cloud": None}, **kw,
    )  # fmt: skip
    assert v1.facts.new_migrations == ("0001",) and v1.plan.deploy.local.steps  # v1 초기 배포
    ok = {k: FileMeta.model_validate(v) for k, v in file_manifest(v1.source).items()}
    commit(repo, V2)  # 새 푸시
    b = plan_deployment(
        request(repo, code_patch=True), run_id="run-v2",
        previous_manifests=lambda p: {"local": ok, "cloud": ok}, **kw,
    )  # fmt: skip
    f = b.facts
    assert f.new_migrations == ("0002",) and f.changed["local"] == {"web": False, "was": True}
    assert {k.name for k in f.env_keys if k.is_new} >= {"SECRET_KEY", "SESSION_COOKIE_SECURE"}
    assert f.infra_inputs_changed and f.db_initialized == {"local": True, "cloud": True}

    check_signals(b.plan)
    assert facts_reader(b.source) == b.plan.facts_hash
    gold = json.loads(GOLDEN.read_text())
    assert b.plan.toggles["code_patch"] is gold["toggles"]["code_patch"] is True
    g, mine = _keys(gold), _keys(b.plan.model_dump(mode="json"))
    only_gold, only_mine = g - mine, mine - g
    # 알려진 차이(05): 골든 deploy.app.cloud(tier=None) 대신 deploy.was.cloud(tier=was)
    assert {k[0] for k in only_gold} == {"deploy_tier"} == {k[0] for k in only_mine}
    assert len(only_gold) == len(only_mine) == 1
    assert g - only_gold == mine - only_mine
