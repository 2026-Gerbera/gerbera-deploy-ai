"""실제 InfraRuntime + fixture 명령 실행기, 승인 저장소, 실행기 연결."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from ddak.app import load_tools
from ddak.cloud.infra import InfraBinding, InfraRuntime, bind_infra, unbind_infra
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Effect, Layer, Source, Target
from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.plan import PlanStep
from ddak.core.contracts.tools.plan_infra import PlanInfraInput
from ddak.core.contracts.tools.validate_infra import ValidateInfraInput
from ddak.core.registry import Registry, spec_for
from ddak.executor.engine import RunStatus, TrackStatus
from ddak.executor.infra import refresh_infra_context
from tests.unit import test_deployment_service as service_tests
from tests.unit.cloud.infra import test_runtime as fixtures
from tests.unit.cloud.test_aws_credentials import SELECTION, StubSessions

rig = service_tests.rig


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [None, "partial_apply", "output", "build", "refresh"])
async def test_registered_infra_tools_prepare_approve_apply_refresh(rig, tmp_path, failure):
    service, source, calls = rig
    previous = service_tests.seed_releases(service)
    loaded = load_tools()
    names = ("validate_infra", "plan_infra", "apply_infra")
    combined = Registry([*service.registry.specs, *(spec_for(n) for n in names)])
    applied = asyncio.Event()

    async def fail_build(inp: service_tests.Input, ctx: RunContext) -> service_tests.Output:
        await applied.wait()
        raise DdakToolError(service_tests.ErrorCode.ADAPTER_FAILED, "fixture build failed")

    for name in service.registry.registered():
        combined.tool(name)(
            fail_build
            if name == "build_image" and failure == "build"
            else service.registry.get(name).fn
        )
    for name in names:
        combined.tool(name)(loaded.get(name).fn)
    service.registry = combined

    def refresh(step, output, context):
        updated = refresh_infra_context(step, output, context)
        if step.tool == "apply_infra":
            applied.set()
            if failure == "refresh":
                raise DdakToolError(
                    service_tests.ErrorCode.INFRA_MISSING,
                    "fixture refresh failed",
                    needs_human=True,
                )
        return updated

    service.refresh = refresh
    p = service_tests.plan()
    ctx = RunContext(p.run_id, project=p.project)
    runner = fixtures.FakeRunner()
    runner.raw = json.loads(json.dumps(runner.raw).replace("flaskr", "demo"))
    arn = fixtures.SECRET.replace("flaskr", "demo").replace("??????", "ABC123")
    runner.outputs = {"app_secret_arn_SECRET_KEY": {"value": arn, "sensitive": False}}
    if failure == "partial_apply":
        runner.apply_code = 1
    elif failure == "output":
        runner.outputs["app_secret_arn_SECRET_KEY"]["sensitive"] = True
    settings = replace(
        fixtures.SETTINGS,
        project=p.project,
        outputs={"app_secret_arn_SECRET_KEY": ("aws_secretsmanager_secret.session.arn", "string")},
    )
    checks = []

    def guard():
        checks.append(True)

    runtime = InfraRuntime(
        aws_project_settings=SELECTION,
        session_factory=StubSessions(),
        root=tmp_path / "infra",
        run_id=p.run_id,
        settings=settings,
        lock_file=b"fixture-lock",
        approvals=lambda: service.get_approvals(p.run_id),
        guard=guard,
        runner=runner,
    )
    analyzer = Mock(validate_policy=Mock(return_value={"findings": []}))
    bind_infra(
        InfraBinding(
            runtime,
            {"app.tf": fixtures.HCL},
            AdapterMode.FAKE,
            lambda: fixtures.SESSION,
            lambda: fixtures.SESSION,
            lambda: analyzer,
        )
    )
    try:
        validated = combined.get("validate_infra").fn(ValidateInfraInput(run_id=p.run_id), ctx)
        assert validated.passed and validated.source is Source.FIXTURE
        planned = combined.get("plan_infra").fn(PlanInfraInput(run_id=p.run_id), ctx)
        assert planned.source is Source.FIXTURE
        p.deploy.cloud.steps.insert(
            0,
            PlanStep(
                id="deploy.infra.cloud",
                tool="apply_infra",
                target=Target.CLOUD,
                layer=Layer.CONDITIONAL,
                effect=Effect.STATE_CHANGE,
                signal="infra_ready",
            ),
        )
        service.prepare(
            p, ctx, source, subjects={"infra": planned.plan_sha256}, infra_summary=planned.summary
        )
        assert service.approval_view(p.run_id)["infra_summary"] == planned.summary
        service.approve(p.run_id, approver="operator")
        service.start(p.run_id)
        result = await service.wait(p.run_id)
        if failure == "build":
            assert result.status is RunStatus.FAILED_BEFORE_DEPLOY
            assert result.tracks["local"] is result.tracks["cloud"] is TrackStatus.SKIPPED
            assert not any(name.startswith(("deploy.", "rollback.")) for name, _ in calls.contexts)
            assert result.infra_changes[0]["plan_sha256"] == planned.plan_sha256
            assert service.get_release(p.run_id)["infra_changes"] == result.infra_changes
            assert service.get_environments(p.project)["cloud"]["current"] == previous["cloud"]
            assert any(e["type"] == "step.skipped" for e in service.events(p.run_id))
            return
        if failure:
            assert result.status is RunStatus.NEEDS_HUMAN
            assert result.tracks["local"] is TrackStatus.DONE
            assert result.tracks["cloud"] is TrackStatus.ROLLBACK_FAILED
            assert not any(name.startswith("rollback.") for name, _ in calls.contexts)
            assert not any(name == "deploy.cloud" for name, _ in calls.contexts)
            envs = service.get_environments(p.project)
            assert envs["cloud"]["status"] == "NEEDS_HUMAN"
            assert envs["cloud"]["current"] == previous["cloud"]
            assert envs["local"]["status"] == "SUCCEEDED"
            if failure == "refresh":
                assert result.infra_changes[0]["status"] == "applied"
                assert service.get_release(p.run_id)["infra_changes"] == result.infra_changes
                assert (runtime.work / "apply-succeeded").exists()
            else:
                assert not (runtime.work / "apply-succeeded").exists()
                with pytest.raises(DdakToolError, match="불명확"):
                    runtime.close()
            with pytest.raises(DdakToolError, match="환경 상태를 사람이 확인"):
                service.store.acquire(p.project, "next-run")
            with service.store.connection() as db:
                assert db.execute("SELECT COUNT(*) FROM locks").fetchone()[0] == 1
            return
        assert result.status is RunStatus.SUCCEEDED
        assert result.context.platform["cloud"]["app_secret_arn_SECRET_KEY"] == arn
        assert checks and any(argv[1] == "apply" for argv, _ in runner.calls)
        cloud = [ctx for name, ctx in calls.contexts if name == "deploy.cloud"]
        assert cloud[0].platform["cloud"]["app_secret_arn_SECRET_KEY"] == arn
    finally:
        unbind_infra(p.run_id)


def test_unbound_infra_tool_fails_without_sdk_or_terraform():
    tool = load_tools().get("validate_infra")
    with pytest.raises(DdakToolError, match="세션 연결"):
        tool.fn(ValidateInfraInput(run_id="unbound-run"), RunContext("unbound-run", project="demo"))


@pytest.mark.parametrize("nested", [False, True])
def test_refresh_preserves_tls_and_other_tier_settings(nested):
    values = {
        "alb_arn": "prior-alb",
        "certificate_arn": "prior-cert",
        "https_listener_arn": "prior-https",
        "http_listener_arn": "prior-http",
        "app_secret_arn_OLD": "prior-secret",
    }
    platform = {"cloud": values} if nested else dict(values)
    platform["local"] = {"host": "fixture-local"}
    ctx = RunContext("refresh-run", platform=platform)
    arn = fixtures.SECRET.replace("??????", "ABC123")
    step = service_tests.step("infra.cloud", "apply_infra", target=Target.CLOUD)
    refreshed = refresh_infra_context(
        step,
        {"passed": True, "layer": "app", "outputs": {"app_secret_arn_SECRET_KEY": arn}},
        ctx,
    )
    assert all(refreshed.platform["cloud"][k] == v for k, v in values.items())
    assert refreshed.platform["local"] == platform["local"]
    assert "local" not in refreshed.platform["cloud"]
    assert refreshed.platform["cloud"]["app_secret_arn_SECRET_KEY"] == arn
    assert ctx.platform == platform
