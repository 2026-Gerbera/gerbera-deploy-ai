"""가짜 레지스트리 툴과 실제 저장소로 승인부터 환경별 기록까지 연결한다."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ddak.core.config import AdapterMode
from ddak.core.contracts.base import ContractModel, ToolInput
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.events import RunEvent
from ddak.core.contracts.plan import Plan, PlanStep, Section
from ddak.core.contracts.release import ImageArtifact, ImageObservation, ReleaseArtifacts
from ddak.core.registry import Registry, spec_for
from ddak.core.snapshots import digest_bytes, file_manifest, preview
from ddak.executor.engine import RunStatus, TrackStatus
from ddak.executor.service import DeploymentService

pytestmark = pytest.mark.anyio
PATCH = b"--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-VERSION = 1\n+VERSION = 2\n"


class Input(ToolInput):
    target: Target | None = None
    tier: str | None = None
    lock_token: str | None = None


class Output(ContractModel):
    passed: bool = True
    release_artifacts: ReleaseArtifacts | None = None
    observation: ImageObservation | None = None


@dataclass
class Calls:
    contexts: list[tuple[str, RunContext]] = field(default_factory=list)
    fail_cloud: bool = False
    rollback_passed: bool = True
    rollback_delay: float = 0
    rolled_back_tiers: list[str | None] = field(default_factory=list)
    fail_rollback: bool = False
    parity_ok: bool = True
    artifacts: ReleaseArtifacts | None = None
    pause_build: bool = False
    build_started: asyncio.Event = field(default_factory=asyncio.Event)
    resume_build: asyncio.Event = field(default_factory=asyncio.Event)


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[tuple[DeploymentService, Path, Calls]]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_bytes(b"VERSION = 1\n")
    calls = Calls()
    registry = Registry(
        spec_for(name)
        for name in (
            "build_image",
            "deploy_tier",
            "health_check",
            "smoke_test",
            "compare_env_results",
            "rollback_tier",
        )
    )

    @registry.tool("build_image")
    async def build(inp: Input, ctx: RunContext) -> Output:
        calls.contexts.append(("build", ctx))
        calls.build_started.set()
        if calls.pause_build:
            await calls.resume_build.wait()
        return Output(release_artifacts=calls.artifacts)

    @registry.tool("deploy_tier")
    async def deploy(inp: Input, ctx: RunContext) -> Output:
        assert inp.target is not None
        calls.contexts.append((f"deploy.{inp.target.value}", ctx))
        if calls.fail_cloud and inp.target is Target.CLOUD:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "injected cloud failure")
        return Output()

    @registry.tool("health_check")
    @registry.tool("smoke_test")
    async def smoke(inp: Input, ctx: RunContext) -> Output:
        assert inp.target is not None
        calls.contexts.append((f"smoke.{inp.target.value}", ctx))
        if ctx.release_artifacts is None:
            return Output()
        platform = "linux/arm64" if inp.target is Target.LOCAL else "linux/amd64"
        return Output(
            observation=ImageObservation(
                platform=platform,
                platform_digest=ctx.release_artifacts.images["was"].platform_digests[platform],
            )
        )

    @registry.tool("compare_env_results")
    async def compare(inp: Input, ctx: RunContext) -> Output:
        calls.contexts.append(("compare", ctx))
        return Output(passed=calls.parity_ok)

    @registry.tool("rollback_tier")
    async def rollback(inp: Input, ctx: RunContext) -> Output:
        assert inp.target is not None
        calls.contexts.append((f"rollback.{inp.target.value}", ctx))
        calls.rolled_back_tiers.append(inp.tier)
        if calls.rollback_delay:
            await asyncio.sleep(calls.rollback_delay)
            assert ctx.deadline is not None and ctx.deadline > time.monotonic()
        if calls.fail_rollback:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "injected rollback failure")
        return Output(passed=calls.rollback_passed)

    service = DeploymentService(registry, tmp_path / "state")
    try:
        yield service, source, calls
    finally:
        calls.resume_build.set()
        service.close()


def step(sid: str, tool: str, **fields: Any) -> PlanStep:
    spec = spec_for(tool)
    return PlanStep(id=sid, tool=tool, layer=spec.layer, effect=spec.effect, **fields)


def plan(run_id: str = "run-2", *, patch: bool = False) -> Plan:
    return Plan.model_validate(
        {
            "run_id": run_id,
            "project": "demo",
            "toggles": {"code_patch": patch},
            "build": {
                "steps": [step("build.was", "build_image", tier="was")],
                "signal": "images_ready",
            },
            "deploy": {
                target.value: {
                    "steps": [
                        step(
                            f"deploy.was.{target.value}",
                            "deploy_tier",
                            target=target,
                            tier="was",
                            wait_for=[
                                "images_ready" if target is Target.LOCAL else "local_verified"
                            ],
                        ),
                        step(
                            f"verify.smoke.{target.value}",
                            "smoke_test",
                            target=target,
                            tier="was",
                            signal=f"{target.value}_verified",
                        ),
                    ]
                }
                for target in (Target.LOCAL, Target.CLOUD)
            },
            "verify": {
                "steps": [
                    step(
                        "verify.compare",
                        "compare_env_results",
                        wait_for=["local_verified", "cloud_verified"],
                    )
                ]
            },
        }
    )


def prepare(service: DeploymentService, source: Path, *, patch: bool = False) -> str:
    p = plan(patch=patch)
    return service.prepare(
        p,
        RunContext(p.run_id, project=p.project, toggles=p.toggles),
        source,
        patch=PATCH if patch else None,
    )


def seed_releases(service: DeploymentService) -> dict[str, dict[str, Any]]:
    previous = {
        target: {
            "release_id": f"baseline-{target}",
            "source_mode": AdapterMode.FAKE.value,
            "source_files": {"app.py": {"sha256": digest_bytes(b"old"), "executable": False}},
            "images": {"was": "example.test/app@" + digest_bytes(target.encode())},
        }
        for target in ("local", "cloud")
    }
    service.store.create_run("baseline", "demo", digest_bytes(b"baseline"))
    service.store.finish(
        "baseline",
        "SUCCEEDED",
        {},
        {},
        {target: ("SUCCEEDED", release) for target, release in previous.items()},
    )
    return previous


async def test_one_click_approval_start_wait_persists_patched_release(rig: Any) -> None:
    service, source, calls = rig
    approved_files = file_manifest(source)
    calls.pause_build = True
    run_id = prepare(service, source, patch=True)
    view = service.approval_view(run_id)
    assert set(view["subjects"]) == {"deploy", "patch"}
    assert view["patch"] == PATCH.decode()
    assert calls.contexts == []
    records = service.approve(run_id, approver="operator")
    assert len({r.approval_id for r in records}) == 1
    assert {r.kind for r in service.store.approvals(run_id)} == {"deploy", "patch"}
    task = service.start(run_id)
    try:
        await asyncio.wait_for(calls.build_started.wait(), timeout=5)
        # 빌드 사본 생성 뒤의 편집은 승인된 사본이나 마지막 성공 기준에 섞이지 않는다.
        (source / "app.py").write_bytes(b"VERSION = 9\n")
    finally:
        calls.resume_build.set()
        result = await asyncio.wait_for(service.wait(run_id), timeout=5)
    assert result is task.result()
    assert result.status is RunStatus.SUCCEEDED
    build_context = next(ctx for name, ctx in calls.contexts if name == "build")
    assert build_context.build_source is not None
    assert (Path(build_context.build_source) / "app.py").read_bytes() == b"VERSION = 2\n"
    assert (source / "app.py").read_bytes() == b"VERSION = 9\n"
    assert service.store.run(run_id)["status"] == "SUCCEEDED"
    for environment in service.store.environments("demo").values():
        assert environment["current"]["source_files"] == approved_files
        assert environment["current"]["source"] == view["snapshot"]
    events = service.events(run_id)
    assert events[-1]["status"] == "SUCCEEDED"
    assert [e["seq"] for e in events] == list(range(len(events)))
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM locks").fetchone()[0] == 0


async def test_start_without_approval_has_zero_tool_effects(rig: Any) -> None:
    service, source, calls = rig
    run_id = prepare(service, source)
    with pytest.raises(DdakToolError) as rejected:
        service.start(run_id)
    assert rejected.value.code is ErrorCode.APPROVAL_REQUIRED
    assert calls.contexts == []
    assert service.events(run_id) == []
    assert service.store.environments("demo") == {}
    assert service.store.run(run_id)["status"] == "AWAITING_APPROVAL"


async def test_plan_file_mutated_during_build_blocks_deployment(rig: Any) -> None:
    service, source, calls = rig
    calls.pause_build = True
    run_id = prepare(service, source)
    service.approve(run_id, approver="operator")
    service.start(run_id)
    try:
        await asyncio.wait_for(calls.build_started.wait(), timeout=5)
        path = service.root / "runs" / run_id / "plan.json"
        data = json.loads(path.read_text())
        data["deploy"]["local"]["steps"][0]["reason"] = "changed after approval"
        path.write_text(json.dumps(data))
    finally:
        calls.resume_build.set()
        result = await asyncio.wait_for(service.wait(run_id), timeout=5)
    assert result.status is not RunStatus.SUCCEEDED
    assert not any(name.startswith("deploy.") for name, _ in calls.contexts)
    assert all(row["current"] is None for row in service.store.environments("demo").values())


async def test_build_artifacts_reach_both_deployments_and_final_context(rig: Any) -> None:
    service, source, calls = rig
    image = ImageArtifact(
        ref="example.test/app@" + digest_bytes(b"index"),
        index_digest=digest_bytes(b"index"),
        platform_digests={
            "linux/amd64": digest_bytes(b"amd64"),
            "linux/arm64": digest_bytes(b"arm64"),
        },
    )
    calls.artifacts = ReleaseArtifacts(snapshot=preview(source), images={"was": image})
    p = plan()
    # target은 선택 필드다. 툴 입력은 트랙에서 추론하므로 관측 저장도 같은 대상을 써야 한다.
    for section in (p.deploy.local, p.deploy.cloud):
        section.steps[-1] = section.steps[-1].model_copy(update={"target": None})
    run_id = service.prepare(p, RunContext(p.run_id, project=p.project), source)
    service.approve(run_id, approver="operator")
    service.start(run_id)
    result = await asyncio.wait_for(service.wait(run_id), timeout=5)
    assert result.status is RunStatus.SUCCEEDED
    deployed = [(name, ctx) for name, ctx in calls.contexts if name.startswith("deploy.")]
    assert [name for name, _ in deployed] == ["deploy.local", "deploy.cloud"]
    assert all(ctx.images["was"] == image.ref for _, ctx in deployed)
    assert result.context is not None and result.context.release_artifacts is not None
    assert set(result.context.release_artifacts.observations) == {"local", "cloud"}
    for target, platform in (("local", "linux/arm64"), ("cloud", "linux/amd64")):
        observation = result.context.release_artifacts.observations[target]["was"]
        assert observation.platform_digest == image.platform_digests[platform]
        persisted = service.store.environments("demo")[target]["current"]["artifacts"]
        assert persisted == result.context.release_artifacts.model_dump(mode="json")


@pytest.mark.parametrize("failure", ["cloud", "parity"])
async def test_failure_preserves_environment_specific_success_and_rolls_back_cloud_only(
    rig: Any, failure: str
) -> None:
    service, source, calls = rig
    previous = seed_releases(service)
    calls.fail_cloud = failure == "cloud"
    calls.parity_ok = failure != "parity"
    run_id = prepare(service, source)
    service.approve(run_id, approver="operator")
    service.start(run_id)
    result = await asyncio.wait_for(service.wait(run_id), timeout=5)
    assert result.status is (
        RunStatus.FAILED_CLOUD if failure == "cloud" else RunStatus.PARITY_FAILED
    )
    assert result.tracks["local"] is TrackStatus.DONE
    assert result.tracks["cloud"] is TrackStatus.ROLLED_BACK
    rollbacks = [(name, ctx) for name, ctx in calls.contexts if name.startswith("rollback.")]
    assert [name for name, _ in rollbacks] == ["rollback.cloud"]
    assert rollbacks[0][1].previous_release == previous
    environments = service.store.environments("demo")
    assert environments["local"]["current"]["release_id"] == run_id
    assert environments["local"]["current"]["source_files"] == file_manifest(source)
    assert environments["local"]["previous"] == previous["local"]
    assert environments["cloud"]["current"] == previous["cloud"]
    assert environments["cloud"]["previous"] is None
    assert {row["status"] for row in environments.values()} == {"DIVERGED"}


async def test_source_changed_after_approval_does_not_promote_any_environment(rig: Any) -> None:
    service, source, calls = rig
    seed_releases(service)
    previous = service.store.environments("demo")
    run_id = prepare(service, source)
    service.approve(run_id, approver="operator")
    (source / "app.py").write_bytes(b"VERSION = 9\n")
    service.start(run_id)
    result = await asyncio.wait_for(service.wait(run_id), timeout=5)
    assert result.status is RunStatus.FAILED_BEFORE_DEPLOY
    assert calls.contexts == []
    assert service.store.environments("demo") == previous
    assert service.store.run(run_id)["status"] == "FAILED_BEFORE_DEPLOY"


@pytest.mark.parametrize("verify_failed", [False, True])
async def test_local_only_run_does_not_mark_empty_cloud_section_deployed(
    rig: Any, verify_failed: bool
) -> None:
    service, source, calls = rig
    p = plan()
    p = p.model_copy(
        update={
            "deploy": p.deploy.model_copy(update={"cloud": Section()}),
            "verify": Section(),
        }
    )
    if verify_failed:
        calls.parity_ok = False
        p = p.model_copy(
            update={
                "verify": Section(
                    steps=[
                        step("verify.compare", "compare_env_results", wait_for=["local_verified"])
                    ]
                )
            }
        )
    run_id = service.prepare(p, RunContext(p.run_id, project=p.project), source)
    service.approve(run_id, approver="operator")
    service.start(run_id)
    result = await asyncio.wait_for(service.wait(run_id), timeout=5)
    assert result.status is (RunStatus.PARITY_FAILED if verify_failed else RunStatus.SUCCEEDED)
    assert set(service.store.environments("demo")) == {"local"}
    assert service.store.environments("demo")["local"]["current"]["release_id"] == run_id
    assert not any(name.endswith(".cloud") for name, _ in calls.contexts)
    assert service.store.environments("demo")["local"]["status"] == "SUCCEEDED"
    # 배포하지 않은 cloud 때문에 다음 실행이 막히지 않는다.
    again = p.model_copy(update={"run_id": "run-3"})
    service.prepare(again, RunContext(again.run_id, project=again.project), source)
    service.approve(again.run_id, approver="operator")
    service.start(again.run_id)
    await asyncio.wait_for(service.wait(again.run_id), timeout=5)


async def test_service_rollback_uses_registered_tier_budget_instead_of_engine_default(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, source, calls = rig
    monkeypatch.setattr("ddak.executor.engine._ROLLBACK_TIMEOUT_S", 0.001)
    calls.fail_cloud = True
    calls.rollback_delay = 0.02
    p = plan()
    p.deploy.cloud.steps.insert(
        1,
        step(
            "deploy.web.cloud",
            "deploy_tier",
            target=Target.CLOUD,
            tier="web",
            wait_for=["local_verified"],
        ),
    )
    run_id = service.prepare(p, RunContext(p.run_id, project=p.project), source)
    service.approve(run_id, approver="operator")
    service.start(run_id)
    result = await asyncio.wait_for(service.wait(run_id), timeout=5)
    assert result.status is RunStatus.FAILED_CLOUD
    assert result.tracks["cloud"] is TrackStatus.ROLLED_BACK
    assert calls.rolled_back_tiers == ["web", "was"]


async def test_heartbeat_retries_transient_sqlite_lock_and_keeps_pipeline_running(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, source, calls = rig
    monkeypatch.setattr("ddak.executor.service._HEARTBEAT_INTERVAL_S", 0.001)
    calls.pause_build = True
    attempts = 0
    refreshed = asyncio.Event()
    loop = asyncio.get_running_loop()
    real_heartbeat = service.store.heartbeat

    def heartbeat(*args: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        real_heartbeat(*args)
        loop.call_soon_threadsafe(refreshed.set)

    monkeypatch.setattr(service.store, "heartbeat", heartbeat)
    run_id = prepare(service, source)
    service.approve(run_id, approver="operator")
    service.start(run_id)
    await asyncio.wait_for(refreshed.wait(), timeout=5)
    calls.resume_build.set()
    result = await asyncio.wait_for(service.wait(run_id), timeout=5)
    assert result.status is RunStatus.SUCCEEDED
    assert attempts >= 2


@pytest.mark.parametrize("transient", [False, True])
async def test_persistent_heartbeat_failure_stops_run_and_retains_lock(
    rig: Any, monkeypatch: pytest.MonkeyPatch, transient: bool
) -> None:
    service, source, calls = rig
    monkeypatch.setattr("ddak.executor.service._HEARTBEAT_INTERVAL_S", 0.001)
    calls.pause_build = True
    attempts = 0

    def heartbeat(*args: Any) -> None:
        nonlocal attempts
        attempts += 1
        if transient:
            raise sqlite3.OperationalError("database is locked")
        raise DdakToolError(ErrorCode.LOCK_INVALID, "injected lost lock")

    monkeypatch.setattr(service.store, "heartbeat", heartbeat)
    run_id = prepare(service, source)
    service.approve(run_id, approver="operator")
    service.start(run_id)
    result = await asyncio.wait_for(service.wait(run_id), timeout=5)
    assert attempts == (3 if transient else 1)
    assert result.status is RunStatus.NEEDS_HUMAN
    assert service.store.run(run_id)["status"] == "NEEDS_HUMAN"
    assert not any(name.startswith(("deploy.", "rollback.")) for name, _ in calls.contexts)
    assert service.events(run_id)[-1]["status"] == "NEEDS_HUMAN"
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM locks").fetchone()[0] == 1


async def test_inflight_heartbeat_error_is_observed_before_success_is_sealed(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, source, calls = rig
    monkeypatch.setattr("ddak.executor.service._HEARTBEAT_INTERVAL_S", 0.001)
    calls.pause_build = True
    entered = asyncio.Event()
    finalizing = asyncio.Event()
    finish_heartbeat = threading.Event()
    loop = asyncio.get_running_loop()

    def heartbeat(*args: Any) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert finish_heartbeat.wait(timeout=5)
        raise DdakToolError(ErrorCode.LOCK_INVALID, "injected late heartbeat failure")

    async def observer(event: RunEvent) -> None:
        if event.status == "FINALIZING":
            finalizing.set()

    monkeypatch.setattr(service.store, "heartbeat", heartbeat)
    run_id = prepare(service, source)
    service.approve(run_id, approver="operator")
    unsubscribe = service.subscribe(run_id, observer)
    task = service.start(run_id)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        calls.resume_build.set()
        await asyncio.wait_for(finalizing.wait(), timeout=5)
        assert not task.done()
    finally:
        finish_heartbeat.set()
        unsubscribe()
    result = await asyncio.wait_for(service.wait(run_id), timeout=5)
    assert result.status is RunStatus.NEEDS_HUMAN
    assert service.store.run(run_id)["status"] == "NEEDS_HUMAN"
    assert all(event["status"] != "SUCCEEDED" for event in service.events(run_id))
    assert service.events(run_id)[-1]["status"] == "NEEDS_HUMAN"
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM locks").fetchone()[0] == 1


async def test_declined_run_stays_closed_and_fresh_run_uses_stored_rollback_baseline(
    rig: Any,
) -> None:
    service, source, calls = rig
    previous = seed_releases(service)
    declined = prepare(service, source)
    records = service.approve(declined, approver="operator", approved=False)
    assert {r.decision for r in records} == {"denied"}
    with pytest.raises(DdakToolError) as blocked:
        service.start(declined)
    assert blocked.value.code is ErrorCode.APPROVAL_REQUIRED
    with pytest.raises(DdakToolError) as terminal:
        service.approve(declined, approver="operator")
    assert terminal.value.code is ErrorCode.PRECONDITION_FAILED
    assert calls.contexts == []
    assert service.store.run(declined)["status"] == "FAILED_BEFORE_DEPLOY"

    p = plan("fresh-run")
    context = RunContext(
        p.run_id,
        project=p.project,
        previous_release={"cloud": {"release_id": "stale-caller-baseline"}},
    )
    fresh = service.prepare(p, context, source)
    service.approve(fresh, approver="operator")
    calls.fail_cloud = True
    service.start(fresh)
    result = await asyncio.wait_for(service.wait(fresh), timeout=5)
    assert result.status is RunStatus.FAILED_CLOUD
    rollback_context = next(ctx for name, ctx in calls.contexts if name == "rollback.cloud")
    assert rollback_context.previous_release == previous
    assert service.store.environments("demo")["cloud"]["current"] == previous["cloud"]
    assert {ctx.run_id for _, ctx in calls.contexts} == {fresh}


async def test_real_update_cannot_use_fake_or_caller_supplied_restore_baseline(rig: Any) -> None:
    service, source, calls = rig
    previous = seed_releases(service)
    p = plan()
    for target, section in ((Target.LOCAL, p.deploy.local), (Target.CLOUD, p.deploy.cloud)):
        section.steps.insert(
            -1, step(f"verify.health.{target.value}", "health_check", target=target, tier="was")
        )
    context = RunContext(
        p.run_id,
        project=p.project,
        adapter_mode=AdapterMode.REAL,
        previous_release={
            target: {**release, "source_mode": AdapterMode.REAL.value}
            for target, release in previous.items()
        },
    )
    run_id = service.prepare(p, context, source)
    service.approve(run_id, approver="operator")
    with pytest.raises(DdakToolError) as blocked:
        service.start(run_id)
    assert blocked.value.code is ErrorCode.PRECONDITION_FAILED
    assert calls.contexts == []
    assert service.store.run(run_id)["status"] == "APPROVED"
    assert {
        target: row["current"] for target, row in service.store.environments("demo").items()
    } == previous
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM locks").fetchone()[0] == 0


async def test_rollback_passed_false_needs_human_and_retains_lock(rig: Any) -> None:
    service, source, calls = rig
    seed_releases(service)
    calls.fail_cloud = True
    calls.rollback_passed = False
    run_id = prepare(service, source)
    service.approve(run_id, approver="operator")
    service.start(run_id)
    result = await asyncio.wait_for(service.wait(run_id), timeout=5)
    assert result.status is RunStatus.NEEDS_HUMAN
    assert result.tracks["cloud"] is TrackStatus.ROLLBACK_FAILED
    assert [name for name, _ in calls.contexts if name.startswith("rollback.")] == [
        "rollback.cloud"
    ]
    assert service.store.run(run_id)["status"] == "NEEDS_HUMAN"
    assert result.context is not None
    service.store.check_lock("demo", run_id, result.context.lock_token)


async def test_cloud_rollback_exception_keeps_successful_local_v2_with_needs_human(
    rig: Any,
) -> None:
    service, source, calls = rig
    previous = seed_releases(service)
    calls.fail_cloud = calls.fail_rollback = True
    run_id = prepare(service, source)
    service.approve(run_id, approver="operator")
    service.start(run_id)
    result = await asyncio.wait_for(service.wait(run_id), timeout=5)
    assert result.status is RunStatus.NEEDS_HUMAN
    assert result.tracks["local"] is TrackStatus.DONE
    assert result.tracks["cloud"] is TrackStatus.ROLLBACK_FAILED
    environments = service.store.environments("demo")
    assert environments["local"]["status"] == "NEEDS_HUMAN"
    assert environments["local"]["current"]["release_id"] == run_id
    assert environments["local"]["current"]["source_files"] == file_manifest(source)
    assert environments["local"]["previous"] == previous["local"]
    assert environments["cloud"]["status"] == "NEEDS_HUMAN"
    assert environments["cloud"]["current"] == previous["cloud"]
    assert not any(name == "rollback.local" for name, _ in calls.contexts)


async def test_shutdown_immediately_after_start_cancels_without_tools_or_retained_lock(
    rig: Any,
) -> None:
    service, source, calls = rig
    run_id = prepare(service, source)
    service.approve(run_id, approver="operator")
    task = service.start(run_id)
    # wait_for나 sleep으로 제어권을 넘기지 않고 task 최초 실행 전에 종료한다.
    await service.shutdown()
    assert task.cancelled()
    assert service.store.run(run_id)["status"] == "CANCELLED"
    assert calls.contexts == []
    assert service.events(run_id) == []
    assert service.store.environments("demo") == {}
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM locks").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM releases").fetchone()[0] == 0


async def test_finish_integrity_error_returns_needs_human_and_retains_lock(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, source, _calls = rig
    seed_releases(service)
    previous = service.store.environments("demo")
    run_id = prepare(service, source)
    service.approve(run_id, approver="operator")
    attempts: list[str] = []
    received: list[RunEvent] = []
    events_before_seal: list[dict[str, Any]] = []
    received_before_seal: list[RunEvent] = []

    async def subscriber(event: RunEvent) -> None:
        received.append(event)

    unsubscribe = service.subscribe(run_id, subscriber)

    def fail_finish(*args: Any, **kwargs: Any) -> None:
        attempts.append(args[0])
        events_before_seal.extend(service.events(run_id))
        received_before_seal.extend(received)
        raise sqlite3.IntegrityError("injected finish failure")

    monkeypatch.setattr(service.store, "finish", fail_finish)
    service.start(run_id)
    try:
        result = await asyncio.wait_for(service.wait(run_id), timeout=5)
    finally:
        unsubscribe()
    assert attempts == [run_id]
    assert result.status is RunStatus.NEEDS_HUMAN
    assert result.tracks["local"] is TrackStatus.DONE
    assert result.tracks["cloud"] is TrackStatus.DONE
    assert service.store.run(run_id)["status"] == "NEEDS_HUMAN"
    assert service.store.environments("demo") == previous
    assert result.context is not None
    service.store.check_lock("demo", run_id, result.context.lock_token)
    assert not (service.root / "runs" / run_id / "release.json").exists()
    assert events_before_seal[-1]["status"] == "FINALIZING"
    assert received_before_seal[-1].status == "FINALIZING"
    assert all(event["status"] != "SUCCEEDED" for event in events_before_seal)
    assert all(event.status != "SUCCEEDED" for event in received_before_seal)
    events = service.events(run_id)
    assert events[-1]["type"] == "run.state"
    assert events[-1]["status"] == "NEEDS_HUMAN"
    assert events[-1]["seq"] == events_before_seal[-1]["seq"] + 1
    assert received[-1].model_dump(mode="json") == events[-1]
    assert all(event["status"] != "SUCCEEDED" for event in events)
    assert all(event.status != "SUCCEEDED" for event in received)
