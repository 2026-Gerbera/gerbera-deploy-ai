"""권한 경계 변경 영수증이 실행기·영속 run 결과까지 전파되는 계약."""

import asyncio
import threading

import pytest

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Effect, Layer, Source, Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.infra_evidence import BoundaryPolicyVersion
from ddak.core.contracts.plan import PlanStep
from ddak.core.contracts.tools.apply_infra import ApplyInfraInput, ApplyInfraOutput
from ddak.core.registry import Registry, spec_for
from ddak.core.runtime import publish_boundary_receipt
from ddak.executor.engine import RunStatus
from tests.unit import test_deployment_service as support

rig = support.rig


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["success", "partial", "cancel", "timeout"])
async def test_boundary_receipts_survive_executor_and_run_persistence(rig, outcome):
    service, source, _ = rig
    support.seed_releases(service)
    receipt = BoundaryPolicyVersion(
        policy_arn="arn:aws:iam::************:policy/ddak/boundary/ddak-app-boundary",
        previous_version_id="v1",
        new_version_id="v2",
        status="updated",
    )
    entered, release = threading.Event(), threading.Event()
    spec = spec_for("apply_infra").model_copy(update={"timeout_s": 1})
    registry = Registry([*service.registry.specs, spec])
    for name in service.registry.registered():
        registry.tool(name)(service.registry.get(name).fn)

    @registry.tool("apply_infra")
    def apply(inp: ApplyInfraInput, ctx: RunContext) -> ApplyInfraOutput:
        if outcome in {"cancel", "timeout"}:
            publish_boundary_receipt(receipt)
            entered.set()
            assert release.wait(5)
        if outcome == "partial":
            raise DdakToolError(
                ErrorCode.ADAPTER_FAILED,
                "fixture: 다음 기반 단계 실패",
                needs_human=True,
                boundary_versions=[receipt],
            )
        return ApplyInfraOutput(
            passed=True,
            layer="platform",
            plan_sha256="sha256:" + "a" * 64,
            outputs={},
            elapsed_seconds=0,
            source=Source.FIXTURE,
            boundary_versions=[receipt],
        )

    service.registry = registry
    service.refresh = lambda step, output, context: context
    plan = support.plan()
    plan.deploy.cloud.steps.insert(
        0,
        PlanStep(
            id="deploy.infra.cloud",
            tool="apply_infra",
            target=Target.CLOUD,
            layer=Layer.CONDITIONAL,
            effect=Effect.STATE_CHANGE,
        ),
    )
    context = RunContext(plan.run_id, project=plan.project)
    subject = "sha256:" + "a" * 64
    summary = {
        "layer": "platform",
        "plan_sha256": subject,
        "exit_code": 2,
        "headline": "fixture 경계 갱신",
        "counts": {"create": 0, "update": 1, "delete": 0, "replace": 0},
        "destructive": [],
        "iam_diff": [],
        "access_analyzer": {"errors": 0, "security_warnings": 0},
        "checkov": {"passed": True, "failed": []},
        "sensitive_masked": True,
    }
    service.prepare(plan, context, source, subjects={"infra": subject}, infra_summary=summary)
    service.approve(plan.run_id, approver="operator")
    task = service.start(plan.run_id)
    try:
        if outcome in {"cancel", "timeout"}:
            assert await asyncio.to_thread(entered.wait, 2)
            if outcome == "cancel":
                task.cancel()
        result = await asyncio.wait_for(service.wait(plan.run_id), 3)
    finally:
        release.set()
        await asyncio.sleep(0.02)
    partial = outcome != "success"
    assert result.status is (RunStatus.NEEDS_HUMAN if partial else RunStatus.SUCCEEDED)
    assert len(result.infra_changes) == 1
    row = result.infra_changes[0]
    assert row["status"] == ("partial" if partial else "applied")
    assert row["boundary_versions"] == [receipt.model_dump(mode="json")]
    assert service.get_release(plan.run_id)["infra_changes"] == result.infra_changes
