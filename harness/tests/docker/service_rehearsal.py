"""실 provider를 canonical Registry/DeploymentService로 연결하는 로컬 리허설.

이미지는 사전 빌드된 테스트 fixture를 재사용한다. AI/C2 빌드/cloud/O3 기능 검증은
실제 구현이라고 주장하지 않는다. health/smoke는 테스트 전용 실제 HTTP 검사다.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
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
from ddak.onprem.deploy import OnPremProvider, reset_demo


class _HttpInput(ToolInput):
    target: Target


class _HttpOutput(ContractModel):
    passed: bool
    source: str = "rehearsal"


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
    http: Callable[..., dict],
) -> list[dict[str, Any]]:
    """source=rehearsal. 한 장부에서 초기 배포 1회·연속 변경·실패·reset·재배포."""
    source = directory / "source"
    shutil.copytree(Path(__file__).parent / "fixture", source)
    state_root = directory / "state"
    reports = []
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
    state = {"expected": "v1", "secret": False, "force_failure": False}

    @registry.tool("health_check")
    def health(inp: _HttpInput, ctx: RunContext) -> _HttpOutput:
        del inp
        result = OnPremProvider().health_check(ctx)
        if not result.passed:
            return _HttpOutput(passed=False)
        http(port, state["expected"], secret_valid=state["secret"], release_id=ctx.run_id)
        return _HttpOutput(passed=not state["force_failure"])

    @registry.tool("smoke_test")
    def smoke(inp: _HttpInput, ctx: RunContext) -> _HttpOutput:
        del inp
        http(port, state["expected"], secret_valid=state["secret"], release_id=ctx.run_id)
        return _HttpOutput(passed=True)

    service = DeploymentService(registry, state_root)

    async def run(rid: str, version: int, mode: RunMode):
        state["expected"] = "v1" if version == 0 else "v2"
        state["secret"] = version != 0
        # 승인할 코드 변경도 매번 다르다. 빌드 provenance는 사전 준비 fixture 대역이다.
        with (source / "server.py").open("a") as stream:
            stream.write(f"\n# rehearsal revision: {rid}\n")
        binding = preview(source)
        steps = [
            _step(
                "deploy.config.local",
                "inject_env_config",
                keys=[] if version == 0 else ["SECRET_KEY"],
            ),
            _step("deploy.db.local", "prepare_db", migrations=["001_fixture"]),
            _step("deploy.was.local", "deploy_tier", tier="was"),
            _step("verify.health.local", "health_check"),
            _step("verify.smoke.local", "smoke_test"),
        ]
        plan = Plan.model_validate(
            {
                "run_id": rid,
                "project": ctx.project,
                "mode": mode,
                "deploy": {"local": {"steps": steps, "signal": "local_verified"}},
            }
        )
        context = replace(
            ctx,
            run_id=rid,
            mode=mode,
            deadline=None,
            previous_release={},
            images={"was": artifacts[version].ref},
            release_artifacts=ReleaseArtifacts(
                snapshot=binding, images={"was": artifacts[version]}
            ),
        )
        started = time.monotonic()
        service.prepare(plan, context, source)
        approval_started = time.monotonic()
        service.approve(rid, approver="runtime-test-fixture")
        approval_wait = time.monotonic() - approval_started
        service.start(rid)
        result = await service.wait(rid)
        reports.append(
            {
                "run_id": rid,
                "mode": mode.value,
                "status": result.status.value,
                "seconds": round(time.monotonic() - started, 2),
                "approval_seconds": round(approval_wait, 3),
                "source": "rehearsal",
                "approval": "fixture 자동 승인; 사람 대기 시간 아님",
                "local_track": result.tracks["local"].value,
                "step_times": {r.tool: round(r.elapsed_s, 3) for r in result.records},
                "images": "prebuilt fixture; not C2 provenance",
                "cloud": "not run",
                "ai": "not run",
                "replicas": ctx.platform["onprem"]["tiers"]["was"].get("replicas", 1),
                "ports": ctx.platform["onprem"]["tiers"]["was"].get("ports", []),
            }
        )
        return result

    def check_baseline():
        env = service.store.environments(ctx.project)["local"]
        assert env["current"]["source_mode"] == "real"
        assert env["current"]["release_id"] == "runtime-bootstrap"
        return http(port, "v1", secret_valid=False, release_id="runtime-bootstrap")

    async def scenario():
        nonlocal service
        baseline = await run("runtime-bootstrap", 0, RunMode.BOOTSTRAP)
        assert baseline.status is RunStatus.SUCCEEDED, baseline
        check_baseline()
        for i in range(1, 4):
            result = await run(f"runtime-update-{i}", 1, RunMode.UPDATE)
            assert result.status is RunStatus.SUCCEEDED, result
            observed = result.context.release_artifacts.observations["local"]["was"]
            assert observed.platform_digest == artifacts[1].platform_digests[observed.platform]
        successful = service.store.environments(ctx.project)["local"]["current"]
        broken = await run("runtime-broken", 2, RunMode.UPDATE)
        assert broken.status is RunStatus.FAILED_LOCAL, broken
        assert broken.tracks["local"] is TrackStatus.ROLLED_BACK
        assert service.store.environments(ctx.project)["local"]["current"] == successful
        failed_step = next(r for r in broken.records if r.status == "failed")
        assert failed_step.tool == "deploy_tier" and "ADAPTER_FAILED" in failed_step.error
        http(port, "v2", release_id="runtime-update-3")
        # 컨트롤러 독점 잠금을 정상 해제한 뒤 운영 스크립트의 동일 함수를 호출한다.
        service.close()
        old_allow = os.environ.get("ALLOW_DEMO_RESET")
        try:
            os.environ["ALLOW_DEMO_RESET"] = "1"
            reset = reset_demo(
                state_root, ctx.project, dict(ctx.platform["onprem"]), "runtime-bootstrap"
            )
        finally:
            if old_allow is None:
                os.environ.pop("ALLOW_DEMO_RESET", None)
            else:
                os.environ["ALLOW_DEMO_RESET"] = old_allow
        assert reset["status"] == "SUCCEEDED" and reset["secret_key_removed"]
        reports.append(
            {
                "action": "reset",
                "status": reset["status"],
                "replicas": reset["replicas"],
                "source": "rehearsal",
            }
        )
        service = DeploymentService(registry, state_root)
        check_baseline()
        rerun = await run("runtime-after-reset", 1, RunMode.UPDATE)
        assert rerun.status is RunStatus.SUCCEEDED, rerun
        successful = service.store.environments(ctx.project)["local"]["current"]
        state["force_failure"] = True
        failure = await run("runtime-health-failure", 1, RunMode.UPDATE)
        assert failure.status is RunStatus.FAILED_LOCAL, failure
        assert failure.tracks["local"] is TrackStatus.ROLLED_BACK
        assert service.store.environments(ctx.project)["local"]["current"] == successful
        failed_step = next(r for r in failure.records if r.status == "check_failed")
        assert failed_step.tool == "health_check" and failed_step.output["passed"] is False
        http(port, "v2", release_id="runtime-after-reset")

    try:
        asyncio.run(scenario())
    finally:
        service.close()
        # 이 리허설이 관리한 이름만 provider의 소유 라벨 검사 후 정리한다.
        with contextlib.suppress(Exception):
            OnPremProvider().rollback("was", replace(ctx, previous_release={}, deadline=None))
    return reports
