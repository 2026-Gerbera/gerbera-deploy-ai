"""실 provider를 canonical Registry/DeploymentService로 연결하는 로컬 리허설.

이미지는 사전 빌드된 테스트 fixture를 재사용한다. AI/C2 빌드/cloud/O3 기능 검증은
실제 구현이라고 주장하지 않는다. health/smoke는 테스트 전용 실제 HTTP 검사다.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ddak.cd.tools.deploy_tier.tool import deploy_tier
from ddak.cd.tools.inject_env_config.tool import inject_env_config
from ddak.cd.tools.prepare_db.tool import prepare_db
from ddak.cd.tools.rollback_tier.tool import rollback_tier
from ddak.core.contracts.base import ContractModel, ToolInput
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode, Target
from ddak.core.contracts.plan import Plan, PlanStep
from ddak.core.contracts.release import ImageArtifact, ReleaseArtifacts
from ddak.core.registry import Registry, spec_for
from ddak.core.snapshots import preview
from ddak.executor.engine import RunStatus, TrackStatus
from ddak.executor.service import DeploymentService


class _HttpInput(ToolInput):
    target: Target


class _HttpOutput(ContractModel):
    passed: bool
    source: str = "real-http-test-fixture"


def _step(sid: str, tool: str, *, tier: str | None = None, **params: Any) -> PlanStep:
    spec = spec_for(tool)
    return PlanStep(
        id=sid,
        tool=tool,
        target=Target.LOCAL,
        tier=tier,
        layer=spec.layer,
        effect=spec.effect,
        params=params,
    )


def service_rehearsals(
    directory: Path,
    ctx: RunContext,
    artifacts: list[ImageArtifact],
    port: int,
    http: Callable[[int, str], dict],
) -> list[dict[str, Any]]:
    source = directory / "source"
    shutil.copytree(Path(__file__).parent / "fixture", source)
    binding = preview(source)
    reports = []

    async def cycle(index: int, fail_health: bool = False) -> None:
        registry = Registry(
            spec_for(name)
            for name in (
                "deploy_tier",
                "inject_env_config",
                "prepare_db",
                "rollback_tier",
                "health_check",
                "smoke_test",
            )
        )
        for name, fn in (
            ("deploy_tier", deploy_tier),
            ("inject_env_config", inject_env_config),
            ("prepare_db", prepare_db),
            ("rollback_tier", rollback_tier),
        ):
            registry.tool(name)(fn)
        state = {"expected": "v1", "force_failure": False}

        @registry.tool("health_check")
        def health(inp: _HttpInput, ctx: RunContext) -> _HttpOutput:
            del inp, ctx
            http(port, state["expected"])
            return _HttpOutput(passed=not state["force_failure"])

        @registry.tool("smoke_test")
        def smoke(inp: _HttpInput, ctx: RunContext) -> _HttpOutput:
            del inp, ctx
            http(port, state["expected"])
            return _HttpOutput(passed=True)

        service = DeploymentService(registry, directory / f"cycle-{index}")

        async def run(version: int, mode: RunMode):
            run_id = f"runtime-{index}-{version}"
            steps = [
                _step("deploy.config.local", "inject_env_config", keys=["SECRET_KEY"]),
                _step("deploy.db.local", "prepare_db", migrations=["001_fixture"]),
                _step("deploy.was.local", "deploy_tier", tier="was"),
                _step("verify.health.local", "health_check"),
                _step("verify.smoke.local", "smoke_test"),
            ]
            plan = Plan.model_validate(
                {
                    "run_id": run_id,
                    "project": ctx.project,
                    "mode": mode,
                    "deploy": {"local": {"steps": steps, "signal": "local_verified"}},
                }
            )
            context = replace(
                ctx,
                run_id=run_id,
                mode=mode,
                deadline=None,
                previous_release={},
                images={"was": artifacts[version].ref},
                release_artifacts=ReleaseArtifacts(
                    snapshot=binding, images={"was": artifacts[version]}
                ),
            )
            service.prepare(plan, context, source)
            service.approve(run_id, approver="runtime-test-fixture")
            service.start(run_id)
            return await service.wait(run_id)

        try:
            started = time.monotonic()
            baseline = await run(0, RunMode.BOOTSTRAP)
            assert baseline.status is RunStatus.SUCCEEDED, baseline
            assert (
                service.store.environments(ctx.project)["local"]["current"]["source_mode"] == "real"
            )
            bootstrap_http = http(port, "v1")
            bootstrap_elapsed = time.monotonic() - started
            state["expected"] = "v2"
            state["force_failure"] = fail_health
            started = time.monotonic()
            update = await run(1, RunMode.UPDATE)
            elapsed = time.monotonic() - started
            if fail_health:
                assert update.status is RunStatus.FAILED_LOCAL, update
                assert update.tracks["local"] is TrackStatus.ROLLED_BACK
                assert (
                    service.store.environments(ctx.project)["local"]["current"]["images"]["was"]
                    == artifacts[0].ref
                )
                observed = http(port, "v1")
            else:
                assert update.status is RunStatus.SUCCEEDED, update
                assert (
                    update.context.release_artifacts.observations["local"]["was"].platform_digest
                    == artifacts[1].platform_digests[
                        ctx.platform["onprem"]["tiers"]["was"]["platform"]
                    ]
                )
                observed = http(port, "v2")
            reports.append(
                {
                    "cycle": index,
                    "bootstrap_status": baseline.status.value,
                    "bootstrap_seconds": round(bootstrap_elapsed, 2),
                    "update_status": update.status.value,
                    "update_seconds": round(elapsed, 2),
                    "forced_health_failure": fail_health,
                    "local_track": update.tracks["local"].value,
                    "bootstrap_http": bootstrap_http,
                    "final_http": observed,
                    "step_times": {
                        record.tool: round(record.elapsed_s, 3) for record in update.records
                    },
                    "registry": "canonical CD tools",
                    "images": "prebuilt runtime fixture",
                    "source_binding": "test fixture source preview; not C2 provenance",
                    "health_smoke": "HTTP fixture",
                    "cloud": "not run",
                    "ai": "not run",
                }
            )
        finally:
            service.close()

    for index in range(3):
        asyncio.run(cycle(index))
    asyncio.run(cycle(3, fail_health=True))
    return reports
