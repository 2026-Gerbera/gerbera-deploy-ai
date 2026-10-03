"""수정12: 실제 외부 호출 없이 소스 판정·승인 근거·준비 실패·replay를 반증한다."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from ddak.app import load_tools
from ddak.core.ai.providers import AIRequest
from ddak.core.ai.providers.claude import ClaudeJevClient
from ddak.core.ai.providers.replay import ReplayProvider
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.generate_plan import GeneratePlanInput
from ddak.core.contracts.tools.validate_plan import ValidatePlanInput
from ddak.core.runtime import tool_context
from ddak.core.smoke import SMOKE_GROUPS, V2_BOX_MARK
from ddak.plan import plan_deployment
from ddak.plan.intake import FetchPolicy
from ddak.plan.intake.fetch import Checkout
from ddak.plan.planner import generate_plan
from ddak.plan.validate import validate_plan
from ddak.plan.validate.assemble import assemble
from ddak.verify.smoke.logic import GROUPS
from tests.unit import test_deployment_service as support
from tests.unit.plan.analyze.test_analyze import FakeJev
from tests.unit.plan.validate.test_validate import draft, facts

rig = support.rig
HARNESS = Path(__file__).parents[2]


def test_v1_v2_v1_flow_smoke_and_stage_measurements(tmp_path):
    load_tools()
    policy = FetchPolicy(root=tmp_path / "sources")
    for case, expected in (("v1", ["base"]), ("v2", ["base", "v2"]), ("v1-again", ["base"])):

        def fetcher(url, ref, *, run_id, project, policy, case=case):
            source = policy.root / run_id
            (source / "was").mkdir(parents=True)
            (source / "deploy.yaml").write_text("tiers:\n  was:\n    paths: [was]\n")
            (source / "was/index.html").write_text(V2_BOX_MARK if case == "v2" else "<p>v1</p>")
            return Checkout(source, "a" * 40)

        events = []
        bundle = plan_deployment(
            DeployRequest(project="demo", repo_url="https://github.com/o/r", target="local"),
            run_id="run-" + case,
            settings=Settings(ai_retries=0),
            fetch_policy=policy,
            fetcher=fetcher,
            previous_manifests=lambda _: {"local": None, "cloud": None},
            jev_client=FakeJev(0.2),
            record_stage=lambda *args, events=events: events.append(args),
        )
        smoke = next(s for s in bundle.plan.deploy.local.steps if s.tool == "smoke_test")
        assert smoke.params["scenarios"] == expected
        assert "fact:smoke_groups" in smoke.evidence
        assert [e[0] for e in events] == ["intake", "detect", "analyze", "plan", "validate"]
        assert all(e[1] >= 0 and e[2] == "succeeded" for e in events)


def test_invalid_smoke_groups_drop_with_warning_and_never_remove_base():
    load_tools()
    f = facts(smoke_groups=("v2", "arbitrary", "base", "v2"))
    plan = validate_plan(ValidatePlanInput(run_id="run-1", facts=f), RunContext("run-1"))
    assert set(SMOKE_GROUPS) == set(GROUPS)
    for section in (plan.deploy.local, plan.deploy.cloud):
        assert next(s for s in section.steps if s.tool == "smoke_test").params == {
            "scenarios": ["base", "v2"]
        }
    assert [w.code for w in plan.warnings].count("unknown_smoke_group") == 1


def test_approval_basis_uses_saved_facts_and_ai_reason(rig):
    service, source, _ = rig
    p = assemble(
        ValidatePlanInput(
            run_id="run-1", facts=facts(), draft=draft(("deploy.storage.local", True))
        ),
        RunContext("run-1"),
        registered_tools={"prepare_storage"},
    )
    # 검증 계획의 미등록 툴을 실행하지 않고 표시용으로 기존 service fixture 계획에 넣는다.
    base = support.plan()
    base = base.model_copy(
        update={"planner": p.planner, "warnings": p.warnings, "invalidated": p.invalidated}
    )
    base.build.steps[0] = base.build.steps[0].model_copy(
        update={
            "by": p.build.steps[0].by,
            "reason": p.build.steps[0].reason,
            "evidence": p.build.steps[0].evidence,
        }
    )
    base.deploy.local.skipped.extend(p.deploy.local.skipped)
    service.prepare(base, RunContext(base.run_id, project=base.project), source)
    directory = service.root / "runs" / base.run_id
    (directory / "facts.json").write_text(json.dumps(facts().model_dump(mode="json")))
    view = service.approval_view(base.run_id)["decision_basis"]
    assert view["planner"]["provider"] == "replay"
    assert "fact:tree_changed.was" in view["steps"][0]["evidence"]
    assert view["env_classification"]["by_provider"]["rule"][0]["kind"] == "secret"
    assert view["env_classification"]["facts_available"]
    assert view["skipped"]
    # optional의 AI 이유가 조립 중 사라지지 않는다.
    assert next(s for s in p.deploy.local.steps if s.id == "deploy.storage.local").reason == "t"


def test_prepare_failure_is_timed_without_exception_text(rig):
    service, source, _ = rig
    p = support.plan()
    with pytest.raises(DdakToolError):
        service.prepare(p, RunContext(p.run_id, source_binding=support.preview(source)), source)
    events = service.events(p.run_id)
    assert events[-1]["preparation_stage"] == "prepare"
    assert events[-1]["status"] == "failed" and events[-1]["elapsed_ms"] >= 0
    assert events[-1]["detail"] is None
    with pytest.raises(RuntimeError), service.preparation_stage(p.run_id, "source"):
        raise RuntimeError("do-not-record-this-sensitive-text")
    assert "sensitive" not in json.dumps(service.events(p.run_id))


def test_intake_failure_records_measurement(tmp_path):
    def fail(*args, **kwargs):
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "private failure text")

    events = []
    with pytest.raises(DdakToolError):
        plan_deployment(
            DeployRequest(project="demo", repo_url="https://github.com/o/r", target="local"),
            run_id="run-fail",
            settings=Settings(),
            fetch_policy=FetchPolicy(root=tmp_path),
            fetcher=fail,
            previous_manifests=lambda _: {},
            record_stage=lambda *args, events=events: events.append(args),
        )
    assert events[0][0] == "intake" and events[0][1] >= 0 and events[0][2] == "failed"
    assert "private" not in str(events)


@pytest.mark.anyio
async def test_execution_event_sequences_follow_preparation(rig):
    service, source, _ = rig
    p = support.plan()
    service.record_stage(p.run_id, "intake", 12, "succeeded")
    service.prepare(p, RunContext(p.run_id, project=p.project), source)
    service.approve(p.run_id, approver="fixture")
    service.start(p.run_id)
    await service.wait(p.run_id)
    seq = [e["seq"] for e in service.events(p.run_id)]
    assert seq == sorted(set(seq))
    assert seq == list(range(len(seq)))


@pytest.mark.parametrize("case,scenarios", [("v1", ["base"]), ("v2", ["base", "v2"])])
def test_checked_in_replays_are_explicit(case, scenarios):
    replay_case = runpy.run_path(str(HARNESS / "scripts/o2_replay.py"))["replay_case"]
    result = replay_case(case)
    assert result["source"] == result["analyze_source"] == "replay"
    assert result["planner"]["source"] == "replay"
    assert result["scenarios"] == [scenarios]


@pytest.mark.parametrize("broken", [False, True])
def test_missing_or_corrupt_replay_rule_fallback(tmp_path, broken):
    class CorruptReplay(ReplayProvider):
        def complete(self, req: AIRequest):
            if broken:
                path = self.path_for(req)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{broken")
            return super().complete(req)

    load_tools()
    replay = CorruptReplay(tmp_path)
    judgment = ClaudeJevClient(provider=replay, model="offline-fixture")
    with tool_context("generate_plan", "run-1"):
        result = generate_plan(
            GeneratePlanInput(run_id="run-1", facts=facts()),
            RunContext("run-1"),
            jev_client=judgment,
            provider=replay,
            settings=Settings(ai_retries=0),
        )
    assert result.draft.decisions == ()
    assert result.draft.planner.provider == "rule" and result.draft.planner.fallback
    assert result.source is Source.LIVE  # 새 규칙 계산은 저장된 AI 응답이라고 가장하지 않는다.
