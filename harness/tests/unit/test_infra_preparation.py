"""조립된 C1 번들만 validate→plan→승인 해시로 연결. 실제 Terraform/AWS 없음."""

from types import SimpleNamespace

import pytest

from ddak import app
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.tools.plan_infra import PlanInfraInput, PlanInfraOutput
from ddak.core.contracts.tools.validate_infra import ValidateInfraInput, ValidateInfraOutput
from ddak.core.registry import Registry, spec_for
from ddak.plan.intake import FetchPolicy, WatchTarget
from tests.unit import test_deployment_service as support
from tests.unit.test_approval_meta import HASH, summary

rig = support.rig
pytestmark = pytest.mark.anyio


def infra_plan(run_id="run-infra"):
    p = support.plan(run_id)
    p.deploy.cloud.steps.insert(
        0, support.step("deploy.infra.cloud", "apply_infra", target=Target.CLOUD)
    )
    return p


async def test_existing_bundle_validates_and_binds_plan_hash_to_approval(rig, monkeypatch):
    service, source, _ = rig
    registry = Registry(
        [
            *service.registry.specs,
            *(spec_for(n) for n in ("validate_infra", "plan_infra", "apply_infra")),
        ]
    )
    for name in service.registry.registered():
        registry.tool(name)(service.registry.get(name).fn)
    calls = []

    @registry.tool("validate_infra")
    def validate(inp: ValidateInfraInput, ctx: RunContext) -> ValidateInfraOutput:
        calls.append("validate")
        return ValidateInfraOutput(passed=True)

    @registry.tool("plan_infra")
    def plan(inp: PlanInfraInput, ctx: RunContext) -> PlanInfraOutput:
        calls.append("plan")
        return PlanInfraOutput(passed=True, plan_sha256=HASH, summary=summary())

    @registry.tool("apply_infra")
    def apply(inp: support.Input, ctx: RunContext) -> support.Output:
        pytest.fail("approval preparation must never apply infrastructure")

    service.registry = registry
    service.refresh = lambda step, output, ctx: ctx
    monkeypatch.setattr(app, "has_infra_binding", lambda rid: True)
    p = infra_plan()
    ctx = RunContext(p.run_id, project=p.project)
    subjects, metadata = await app._infra_approval(service, p, ctx)
    rid = service.prepare(p, ctx, source, subjects=subjects, infra_summary=metadata)
    assert calls == ["validate", "plan"]
    assert service.approval_view(rid)["subjects"]["infra"] == HASH
    assert service.approval_view(rid)["infra_summary"] == summary()
    assert {r.kind for r in service.approve(rid, approver="operator")} == {"infra", "deploy"}


async def test_missing_generator_bundle_is_visible_named_failure(rig, monkeypatch):
    service, source, _ = rig
    monkeypatch.setattr(app, "has_infra_binding", lambda rid: False)

    def plan(request, **kwargs):
        p = infra_plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
        return SimpleNamespace(
            plan=p,
            context=RunContext(p.run_id, project=p.project, mode=p.mode),
            source=source,
        )

    monkeypatch.setattr(app, "plan_deployment", plan)
    rid = await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", "https://github.com/org/app", "prod"),
        "a" * 40,
        policy=FetchPolicy(root=source.parent),
    )
    row = service.get_run(rid)
    assert row["status"] == "FAILED_BEFORE_DEPLOY"
    assert row["result"]["phase"] == "infra"
    assert "generate_infra" in row["result"]["detail"]
    assert not service.get_approvals(rid)


async def test_local_selection_does_not_require_cloud_bundle(rig, monkeypatch):
    service, _, _ = rig
    monkeypatch.setattr(app, "has_infra_binding", lambda rid: pytest.fail("unselected cloud"))
    p = infra_plan()
    assert await app._infra_approval(
        service, p, RunContext(p.run_id, project=p.project, targets="onprem")
    ) == ({}, None)
