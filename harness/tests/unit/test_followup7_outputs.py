"""두 층 출력 → 승인/apply → 저장·재시작 → 인프라 변경 없는 다음 run."""

from types import SimpleNamespace

import pytest

from ddak import app
from ddak.cloud.deploy._platform import secret_ids
from ddak.core.config import AdapterMode, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.infra_outputs import checked_outputs
from ddak.core.contracts.tools.apply_infra import ApplyInfraInput, ApplyInfraOutput
from ddak.core.registry import Registry, spec_for
from ddak.executor.engine import RunStatus
from ddak.executor.infra import refresh_infra_context
from ddak.executor.service import DeploymentService
from ddak.plan.intake import FetchPolicy, WatchTarget
from tests.unit import test_deployment_service as support
from tests.unit.test_approval_meta import HASH, summary

rig = support.rig
pytestmark = pytest.mark.anyio
ACCOUNT = "123456789012"
PLATFORM = {
    "cluster_name": "fixture-cluster",
    "ecs_service_name": "fixture-service",
    "public_subnet_ids": ["subnet-fixture"],
    "app_security_group_id": "sg-fixture",
    "target_group_arn": (
        f"arn:aws:elasticloadbalancing:ap-northeast-2:{ACCOUNT}:targetgroup/app/abc"
    ),
    "codebuild_project_name": "fixture-build",
    "image_repository": "fixture/app",
}
APP = {
    "app_secret_arn_SECRET_KEY": (
        f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:ddak/demo/SECRET_KEY-ABC123"
    ),
    "task_execution_role_arn": f"arn:aws:iam::{ACCOUNT}:role/ddak/app/exec",
    "task_role_arn": f"arn:aws:iam::{ACCOUNT}:role/ddak/app/task",
    "dbinit_execution_role_arn": f"arn:aws:iam::{ACCOUNT}:role/ddak/app/dbinit",
}


async def test_apply_then_no_apply_preserves_both_output_layers_after_restart(rig, monkeypatch):
    service, source, _calls = rig
    registry = Registry([*service.registry.specs, spec_for("apply_infra")])
    for name in service.registry.registered():
        registry.tool(name)(service.registry.get(name).fn)
    applied = []

    @registry.tool("apply_infra")
    def apply(inp: ApplyInfraInput, ctx: RunContext) -> ApplyInfraOutput:
        applied.append(ctx.run_id)
        return ApplyInfraOutput(
            passed=True,
            layer="app",
            outputs=APP,
            plan_sha256=HASH,
            elapsed_seconds=0,
            source="fixture",
        )

    service.registry = registry
    service.refresh = refresh_infra_context
    p = support.plan("with-apply")
    step = support.step("deploy.infra.cloud", "apply_infra", target=Target.CLOUD)
    p.deploy.cloud.steps.insert(0, step)
    ctx = refresh_infra_context(
        step,
        {"passed": True, "layer": "platform", "outputs": checked_outputs(PLATFORM, "platform")},
        RunContext(p.run_id, project=p.project, targets="cloud"),
    )
    service.prepare(p, ctx, source, subjects={"infra": HASH}, infra_summary=summary())
    service.approve(p.run_id, approver="operator")
    service.start(p.run_id)
    assert (await service.wait(p.run_id)).status is RunStatus.SUCCEEDED
    expected = {**PLATFORM, **APP}
    assert service.get_release(p.run_id)["platform_outputs"] == expected
    assert service.get_environments("demo")["cloud"]["current"]["platform_outputs"] == expected
    root = service.root
    service.close()
    restarted = DeploymentService(registry, root, refresh=refresh_infra_context)
    try:
        assert restarted.get_platform_outputs("demo", AdapterMode.FAKE) == expected
        assert restarted.get_platform_outputs("demo", AdapterMode.REAL) == {}
        seen = []

        def plan(request, **kwargs):
            seen.append(kwargs["platform"]["cloud"])
            p = support.plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
            return SimpleNamespace(
                plan=p, context=RunContext(p.run_id, project=p.project, mode=p.mode), source=source
            )

        monkeypatch.setattr(app, "plan_deployment", plan)
        rid = await app._prepare_commit(
            restarted,
            Settings(),
            WatchTarget("demo", "https://github.com/fixture/app", "prod", "cloud"),
            "a" * 40,
            policy=FetchPolicy(root=source.parent),
        )
        assert seen == [{**expected, "region": "ap-northeast-2"}]
        prepared = restarted._load_prepared(rid)
        assert not any(s.tool == "apply_infra" for s in prepared.plan.deploy.cloud.steps)
        assert secret_ids(prepared.context) == {"SECRET_KEY": APP["app_secret_arn_SECRET_KEY"]}
        restarted.approve(rid, approver="operator")
        restarted.start(rid)
        result = await restarted.wait(rid)
        assert result.status is RunStatus.SUCCEEDED
        assert applied == ["with-apply"]
        assert restarted.get_release(rid)["platform_outputs"] == expected
        assert secret_ids(result.context) == secret_ids(prepared.context)
        assert "region" not in restarted.get_platform_outputs("demo", AdapterMode.FAKE)
    finally:
        restarted.close()


async def test_first_cloud_request_gets_region_without_terraform_output(rig, monkeypatch):
    service, source, _ = rig
    seen = []

    def plan(request, **kwargs):
        seen.append(kwargs["platform"])
        p = support.plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
        return SimpleNamespace(
            plan=p, context=RunContext(p.run_id, project=p.project, mode=p.mode), source=source
        )

    monkeypatch.setattr(app, "plan_deployment", plan)
    rid = await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", "https://github.com/fixture/app", "prod", "cloud"),
        "a" * 40,
        policy=FetchPolicy(root=source.parent),
    )
    assert service.approval_view(rid)["run_id"] == rid
    assert seen == [{"cloud": {"region": "ap-northeast-2"}}]
