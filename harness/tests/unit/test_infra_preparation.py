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


async def test_generated_bundle_is_assembled_validated_approved_and_applied(
    rig, monkeypatch, tmp_path
):
    from dataclasses import replace
    from unittest.mock import Mock

    from ddak.cloud.infra import InfraBinding, InfraRuntime, unbind_infra
    from ddak.core.contracts.tools.generate_infra import GenerateInfraInput, GenerateInfraOutput
    from ddak.core.snapshots import digest_bytes
    from ddak.executor.engine import RunStatus
    from ddak.executor.infra import refresh_infra_context
    from tests.unit.cloud.infra import test_runtime as fixtures

    service, source, _ = rig
    loaded = app.load_tools()
    names = ("generate_infra", "validate_infra", "plan_infra", "apply_infra")
    registry = Registry([*service.registry.specs, *(spec_for(n) for n in names)])
    for name in service.registry.registered():
        registry.tool(name)(service.registry.get(name).fn)
    for name in names[1:]:
        registry.tool(name)(loaded.get(name).fn)

    @registry.tool("generate_infra")
    def generate(inp: GenerateInfraInput, ctx: RunContext) -> GenerateInfraOutput:
        directory = __import__("pathlib").Path(inp.directory)
        (directory / "app.tf").write_text(fixtures.HCL)
        return GenerateInfraOutput(
            directory=str(directory),
            layer=inp.layer,
            files={"app.tf": digest_bytes(fixtures.HCL.encode())},
            source="fixture",
        )

    runners = []

    def factory(bundle, files, ctx, *, root, approvals, guard):
        runner = fixtures.FakeRunner()
        runner.raw = __import__("json").loads(
            __import__("json").dumps(runner.raw).replace("flaskr", "demo")
        )
        runner.outputs = {}
        runtime = InfraRuntime(
            root=root,
            run_id=ctx.run_id,
            settings=replace(fixtures.SETTINGS, project="demo", outputs={}),
            lock_file=b"fixture",
            approvals=approvals,
            guard=guard,
            runner=runner,
        )
        runners.append(runner)
        return InfraBinding(
            runtime,
            files,
            ctx.adapter_mode,
            lambda: fixtures.SESSION,
            lambda: fixtures.SESSION,
            lambda: Mock(validate_policy=Mock(return_value={"findings": []})),
        )

    monkeypatch.setattr(app, "create_binding", factory)
    service.registry = registry
    service.refresh = refresh_infra_context
    p = infra_plan("run-generated")
    ctx = RunContext(p.run_id, project=p.project)
    try:
        subjects, metadata = await app._infra_approval(service, p, ctx)
        assert "HCL source=fixture" in metadata["headline"]
        assert not any(c[0][1] == "apply" for c in runners[0].calls)
        service.prepare(p, ctx, source, subjects=subjects, infra_summary=metadata)
        service.approve(p.run_id, approver="operator")
        service.start(p.run_id)
        assert (await service.wait(p.run_id)).status is RunStatus.SUCCEEDED
        assert any(c[0][1] == "apply" for c in runners[0].calls)
    finally:
        unbind_infra(p.run_id)


async def test_invalid_generated_bundle_is_regenerated_with_validator_feedback(rig, monkeypatch):
    from dataclasses import dataclass

    from ddak.core.contracts.enums import Source
    from ddak.core.contracts.tools.generate_infra import GenerateInfraInput, GenerateInfraOutput
    from ddak.core.snapshots import digest_bytes

    service, _, _ = rig
    names = ("generate_infra", "validate_infra", "plan_infra")
    registry = Registry([*service.registry.specs, *(spec_for(name) for name in names)])
    for name in service.registry.registered():
        registry.tool(name)(service.registry.get(name).fn)

    generated_contexts = []

    @registry.tool("generate_infra")
    def generate(inp: GenerateInfraInput, ctx: RunContext) -> GenerateInfraOutput:
        generated_contexts.append(ctx)
        source = 'resource "aws_s3_bucket" "source" {}\n'
        directory = __import__("pathlib").Path(inp.directory)
        (directory / "main.tf").write_text(source)
        return GenerateInfraOutput(
            directory=str(directory),
            layer=inp.layer,
            files={"main.tf": digest_bytes(source.encode())},
            source=Source.FIXTURE,
        )

    validation_calls = 0

    @registry.tool("validate_infra")
    def validate(inp: ValidateInfraInput, ctx: RunContext) -> ValidateInfraOutput:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 1:
            return ValidateInfraOutput(passed=False, detail="HCL_INVALID")
        return ValidateInfraOutput(passed=True)

    @registry.tool("plan_infra")
    def plan(inp: PlanInfraInput, ctx: RunContext) -> PlanInfraOutput:
        return PlanInfraOutput(passed=True, plan_sha256=HASH, summary=summary())

    @dataclass(frozen=True)
    class Binding:
        generation_source: Source | None = None

    unbound = []
    monkeypatch.setattr(app, "has_infra_binding", lambda _: False)
    monkeypatch.setattr(app, "create_binding", lambda *args, **kwargs: Binding())
    monkeypatch.setattr(app, "bind_infra", lambda _: None)
    monkeypatch.setattr(app, "unbind_infra", unbound.append)
    service.registry = registry
    p = infra_plan("run-regenerate")
    ctx = RunContext(p.run_id, project=p.project)

    subjects, metadata = await app._infra_approval(service, p, ctx)

    assert subjects == {"infra": HASH}
    assert metadata == summary()
    assert len(generated_contexts) == 2
    assert generated_contexts[0].project_settings.get("_infra_validation_feedback") is None
    assert generated_contexts[1].project_settings["_infra_validation_feedback"] == "HCL_INVALID"
    first_directory = generated_contexts[1].project_settings["_infra_repair_directory"]
    assert first_directory.endswith(p.run_id)
    assert not first_directory.endswith("-retry-2")
    assert unbound == [p.run_id]


async def test_sync_generator_timeout_never_creates_binding(rig, monkeypatch):
    import time

    from ddak.core.contracts.errors import DdakToolError
    from ddak.core.contracts.tools.generate_infra import GenerateInfraInput, GenerateInfraOutput

    service, _, _ = rig
    registry = Registry(
        [*service.registry.specs, spec_for("generate_infra").model_copy(update={"timeout_s": 0.01})]
    )

    @registry.tool("generate_infra")
    def slow(inp: GenerateInfraInput, ctx: RunContext) -> GenerateInfraOutput:
        time.sleep(0.05)
        raise RuntimeError("late fixture result must not be used")

    service.registry = registry
    monkeypatch.setattr(app, "bind_infra", lambda *_: pytest.fail("must not bind timeout result"))
    p = infra_plan("generator-timeout")
    with pytest.raises(DdakToolError, match="generate_infra 제한 시간"):
        await app._infra_approval(service, p, RunContext(p.run_id, project=p.project))
