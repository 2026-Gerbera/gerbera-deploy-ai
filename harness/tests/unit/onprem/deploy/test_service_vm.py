"""source=fake: 실제 서비스·저장소·provider와 가짜 VM의 부분 실패 통합 검증."""

import asyncio
from dataclasses import replace

from ddak.cd.tools.deploy_tier.tool import deploy_tier
from ddak.cd.tools.inject_env_config.tool import inject_env_config
from ddak.cd.tools.prepare_db.tool import prepare_db
from ddak.cd.tools.rollback_tier.tool import rollback_tier
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.plan import Plan
from ddak.core.contracts.release import ImageArtifact, ReleaseArtifacts
from ddak.core.registry import Registry, spec_for
from ddak.core.snapshots import preview
from ddak.executor.engine import RunStatus, TrackStatus
from ddak.executor.service import DeploymentService
from tests.docker.service_rehearsal import _HttpInput, _HttpOutput, _step
from tests.unit.onprem.deploy.test_onprem_vm import vm as vm


def test_service_partial_v2_failure_rolls_back_1_2_preserving_3_and_success_ledger(
    vm, tmp_path, monkeypatch
):
    import ddak.cd.dispatch as dispatch

    fake, provider, ctx = vm
    ctx.platform["onprem"]["tiers"]["was"]["replicas"] = 3
    monkeypatch.setattr(dispatch, "OnPremProvider", lambda: provider)
    rollbacks = []
    original_rollback = provider.rollback

    def rollback(tier, context):
        result = original_rollback(tier, context)
        rollbacks.append(result)
        return result

    monkeypatch.setattr(provider, "rollback", rollback)
    source = tmp_path / "source"
    source.mkdir()
    registry = Registry(
        spec_for(name)
        for name in (
            "inject_env_config",
            "prepare_db",
            "deploy_tier",
            "rollback_tier",
            "health_check",
            "smoke_test",
        )
    )
    for name, fn in (
        ("inject_env_config", inject_env_config),
        ("prepare_db", prepare_db),
        ("deploy_tier", deploy_tier),
        ("rollback_tier", rollback_tier),
    ):
        registry.tool(name)(fn)

    @registry.tool("health_check")
    @registry.tool("smoke_test")
    def health(inp: _HttpInput, ctx: RunContext) -> _HttpOutput:
        return _HttpOutput(passed=provider.health_check(ctx).passed)

    service = DeploymentService(registry, tmp_path / "state")

    async def run(version):
        rid = f"service-v{version + 1}"
        (source / "app.py").write_text(f"VERSION={version + 1}\n")
        plan = Plan.model_validate(
            {
                "run_id": rid,
                "project": ctx.project,
                "mode": "bootstrap" if version == 0 else "update",
                "deploy": {
                    "local": {
                        "signal": "local_verified",
                        "steps": [
                            _step(
                                "deploy.config.local",
                                "inject_env_config",
                                keys=[] if version == 0 else ["SECRET_KEY"],
                            ),
                            _step("deploy.migrate.local", "prepare_db", migrations=["001_users"]),
                            _step("deploy.was.local", "deploy_tier", tier="was"),
                            _step("verify.health.local", "health_check"),
                            _step("verify.smoke.local", "smoke_test"),
                        ],
                    }
                },
            }
        )
        artifact = ImageArtifact(
            ref=fake.refs[version],
            index_digest=fake.refs[version].split("@")[1],
            platform_digests={
                "linux/arm64": fake.children[version],
                "linux/amd64": fake.children[version],
            },
        )
        context = replace(
            ctx,
            run_id=rid,
            adapter_mode=AdapterMode.REAL,
            mode=RunMode.BOOTSTRAP if version == 0 else RunMode.UPDATE,
            images={"was": artifact.ref},
            release_artifacts=ReleaseArtifacts(snapshot=preview(source), images={"was": artifact}),
        )
        service.prepare(plan, context, source)
        service.approve(rid, approver="fixture")
        service.start(rid)
        return await service.wait(rid)

    async def scenario():
        assert (await run(0)).status is RunStatus.SUCCEEDED
        baseline = service.store.environments(ctx.project)["local"]
        record = service.store.run("service-v1")
        ids = {n: s["Id"] for n, s in fake.containers.items()}
        fake.fail_ready = "app-2"
        result = await run(1)
        assert result.status is RunStatus.FAILED_LOCAL
        assert result.tracks["local"] is TrackStatus.ROLLED_BACK
        after = service.store.environments(ctx.project)["local"]
        assert after["current"] == baseline["current"] and after["previous"] == baseline["previous"]
        assert service.store.run("service-v1") == record
        assert fake.containers["app-3"]["Id"] == ids["app-3"]
        assert all(fake.containers[f"app-{i}"]["Id"] != ids[f"app-{i}"] for i in (1, 2))
        assert all(
            s["Config"]["Image"] == fake.refs[0] and s["State"]["Running"]
            for s in fake.containers.values()
        )
        assert len(rollbacks) == 1 and rollbacks[0].detail == "복구 replica: [1, 2]"

    try:
        asyncio.run(scenario())
    finally:
        service.close()
