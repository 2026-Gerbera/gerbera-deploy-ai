"""가짜 레지스트리 툴과 실제 저장소로 승인부터 환경별 기록까지 연결한다."""

from __future__ import annotations

import asyncio
import hashlib
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
                            wait_for=["images_ready"],
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


def patch_metadata(patch: bytes = PATCH, **overrides: Any) -> dict[str, Any]:
    return {
        "passed": True,
        "patch_sha256": "sha256:" + hashlib.sha256(patch).hexdigest(),
        "reason": "승인된 설정 패치 fixture",
        "reuse": False,
        "source": "fixture",
        **overrides,
    }


def prepare(service: DeploymentService, source: Path, *, patch: bool = False) -> str:
    p = plan(patch=patch)
    return service.prepare(
        p,
        RunContext(p.run_id, project=p.project, toggles=p.toggles),
        source,
        patch=PATCH if patch else None,
        patch_meta=patch_metadata() if patch else None,
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
    assert view["patch"] is None  # 수정11: 원문은 공개 승인 조회에 노출하지 않는다.
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
    assert [(e["type"], e["preparation_stage"]) for e in service.events(run_id)] == [
        ("stage.finished", "prepare")
    ]  # 준비 계측만 있고 툴 실행 이벤트는 없다.
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
    assert {name for name, _ in deployed} == {"deploy.local", "deploy.cloud"}
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
    assert environments["local"]["status"] == "SUCCEEDED"
    assert environments["cloud"]["status"] == "ROLLED_BACK"


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


@pytest.mark.parametrize("diagnostic", ["secret_assignment", "secret_json", "database_url"])
async def test_error_json_redacts_diagnostics_before_persistence(rig, monkeypatch, diagnostic):
    from ddak.core.redact import REDACTED
    from ddak.executor import service as service_module

    service, source, calls = rig
    secret = "fixture-" + "private-value"
    exposed = {
        "secret_assignment": f"SECRET_KEY={secret}",
        "secret_json": json.dumps({"api_key": secret, "operation": "materialize"}),
        "database_url": f"mysql+pymysql://app:{secret}@db.invalid/app",
    }[diagnostic]
    # 예외 문구는 저장하지 않는다. 저장 가능한 type 필드에도 가림을 강제한다.
    failure_type = type(f"FixtureError {exposed}", (RuntimeError,), {})

    def failed_materialize(*args, **kwargs):
        raise failure_type("unlabelled-fixture-private-message")

    run_id = prepare(service, source)
    service.approve(run_id, approver="operator")
    monkeypatch.setattr(service_module, "materialize", failed_materialize)
    service.start(run_id)
    result = await asyncio.wait_for(service.wait(run_id), timeout=5)

    raw = (service.root / "runs" / run_id / "error.json").read_text()
    error = json.loads(raw)
    assert result.status is RunStatus.FAILED_BEFORE_DEPLOY
    assert calls.contexts == []
    assert secret not in raw
    assert "unlabelled-fixture-private-message" not in raw
    assert REDACTED in error["type"]
    assert error["detail"] == "실행 시작/기록 실패; 상태 확인 필요"
    if diagnostic == "secret_json":
        nested = json.loads(error["type"].removeprefix("FixtureError "))
        assert nested == {"api_key": REDACTED, "operation": "materialize"}
    if diagnostic == "database_url":
        assert error["type"] == f"FixtureError mysql+pymysql://app:{REDACTED}@db.invalid/app"


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
    assert result.status is RunStatus.SUCCEEDED
    assert not any(name == "compare" for name, _ in calls.contexts)
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
    p.build.steps.append(step("build.web", "build_image", tier="web"))
    p.deploy.cloud.steps.insert(
        1,
        step(
            "deploy.web.cloud",
            "deploy_tier",
            target=Target.CLOUD,
            tier="web",
            wait_for=["images_ready"],
        ),
    )
    run_id = service.prepare(p, RunContext(p.run_id, project=p.project), source)
    service.approve(run_id, approver="operator")
    service.start(run_id)
    result = await asyncio.wait_for(service.wait(run_id), timeout=5)
    assert result.status is RunStatus.FAILED_CLOUD
    assert result.tracks["cloud"] is TrackStatus.ROLLED_BACK
    assert calls.rolled_back_tiers == ["was"]  # web은 아직 실행되지 않았다


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
        platform={"onprem": {"tiers": {"was": {}}}},
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
    assert environments["local"]["status"] == "SUCCEEDED"
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
    assert [(e["type"], e["preparation_stage"]) for e in service.events(run_id)] == [
        ("stage.finished", "prepare")
    ]  # 준비 계측만 있고 툴 실행 이벤트는 없다.
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
    current = service.store.environments("demo")
    assert all(
        current[t]["current"] == previous[t]["current"]
        and current[t]["previous"] == previous[t]["previous"]
        for t in previous
    )
    assert all(current[t]["status"] == "NEEDS_HUMAN" for t in previous)
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


@pytest.mark.parametrize(
    "targets,expected,omitted", [("onprem", "local", "cloud"), ("cloud", "cloud", "local")]
)
async def test_explicit_target_runs_only_selected_environment_and_records_trigger(
    rig: Any, targets: str, expected: str, omitted: str
) -> None:
    service, source, calls = rig
    previous = seed_releases(service)
    before_env = service.store.environments("demo")
    p = plan()
    original = p.model_dump_json()
    ctx = RunContext(p.run_id, project=p.project, targets=targets, trigger="auto")
    run_id = service.prepare(p, ctx, source)
    view = service.approval_view(run_id)
    assert view["targets"] == targets and view["trigger"] == "auto"
    assert view["plan"]["deploy"][omitted]["steps"] == []
    service.approve(run_id, approver="operator")
    service.start(run_id)
    result = await service.wait(run_id)
    assert result.status is RunStatus.SUCCEEDED
    assert result.tracks[omitted] is TrackStatus.NOT_APPLICABLE
    assert result.tracks[expected] is TrackStatus.DONE
    assert not any(name.endswith("." + omitted) for name, _ in calls.contexts)
    assert not any(name == "compare" for name, _ in calls.contexts)
    env = service.store.environments(p.project)
    assert env[omitted]["current"] == previous[omitted]
    assert env[omitted] == before_env[omitted]
    assert env[expected]["current"]["trigger"] == "auto"
    assert p.model_dump_json() == original
    assert service.store.run(run_id)["result"]["targets"] == targets


@pytest.mark.parametrize("kwargs", [{"targets": "all"}, {"trigger": "schedule"}])
async def test_invalid_run_selection_is_rejected(kwargs: Any) -> None:
    with pytest.raises(ValueError):
        RunContext("bad-selection", **kwargs)


async def test_explicit_both_rejects_incomplete_plan(rig: Any) -> None:
    service, source, _ = rig
    p = plan()
    p = p.model_copy(update={"deploy": p.deploy.model_copy(update={"cloud": Section()})})
    with pytest.raises(DdakToolError, match="선택한 대상"):
        service.prepare(p, RunContext(p.run_id, project=p.project, targets="both"), source)


@pytest.mark.parametrize("changed", [{"targets": "cloud"}, {"trigger": "auto"}])
async def test_refresh_cannot_rewrite_approved_run_selection(rig: Any, changed: Any) -> None:
    from dataclasses import replace

    service, source, calls = rig
    service.refresh = lambda step, output, ctx: replace(ctx, **changed)
    p = plan()
    rid = service.prepare(p, RunContext(p.run_id, project=p.project, targets="onprem"), source)
    service.approve(rid, approver="operator")
    service.start(rid)
    result = await service.wait(rid)
    assert result.status is RunStatus.FAILED_BEFORE_DEPLOY
    assert not any(name.startswith("deploy.") for name, _ in calls.contexts)
    record = service.store.run(rid)["result"]
    assert record["targets"] == "onprem" and record["trigger"] == "manual"


async def test_release_keeps_original_and_candidate_commit_separate(rig: Any) -> None:
    service, source, _calls = rig
    p = plan()
    source_sha, candidate_sha = "a" * 40, "b" * 40
    rid = service.prepare(
        p,
        RunContext(p.run_id, project=p.project, source_sha=source_sha, candidate_sha=candidate_sha),
        source,
    )
    service.approve(rid, approver="operator")
    service.start(rid)
    assert (await service.wait(rid)).status is RunStatus.SUCCEEDED
    for row in service.store.environments("demo").values():
        assert row["current"]["source_sha"] == source_sha
        assert row["current"]["candidate_sha"] == candidate_sha
    assert service.deployment_baselines("demo") == {"local": source_sha, "cloud": source_sha}


@pytest.mark.parametrize("failure", ["none", "cloud", "parity"])
async def test_service_publishes_only_verified_environment_commits(rig: Any, failure: str) -> None:
    from ddak.core.app_repository import AppRepository

    service, source, calls = rig
    calls.fail_cloud = failure == "cloud"
    calls.parity_ok = failure != "parity"
    recorded = []

    class RecordingRepository(AppRepository):
        def validate_candidate(self, *args):
            pass  # Git 게시 오류 처리만 검사하는 fixture

        def publish(self, candidate_sha, selected, succeeded):
            recorded.append((candidate_sha, selected, succeeded))
            return {"status": "FAILED", "error": "fixture"}

    service.repositories["demo"] = RecordingRepository(source, allow_local=True)
    p = plan()
    rid = service.prepare(
        p,
        RunContext(p.run_id, project=p.project, source_sha="a" * 40, candidate_sha="b" * 40),
        source,
    )
    service.approve(rid, approver="operator")
    service.start(rid)
    result = await service.wait(rid)
    expected = {"local", "cloud"} if failure == "none" else {"local"}
    assert recorded == [("b" * 40, {"local", "cloud"}, expected)]
    assert result.status is (
        RunStatus.SUCCEEDED
        if failure == "none"
        else RunStatus.FAILED_CLOUD
        if failure == "cloud"
        else RunStatus.PARITY_FAILED
    )
    assert service.store.environments("demo")["local"]["current"]["git"]["status"] == "FAILED"


@pytest.mark.parametrize("fails", [False, True])
async def test_repeated_cancel_waits_for_git_and_preserves_completed_deployment(
    rig: Any, fails: bool
) -> None:
    from ddak.core.app_repository import AppRepository

    service, source, _calls = rig
    entered, release = threading.Event(), threading.Event()

    class PausedRepository(AppRepository):
        def validate_candidate(self, *args):
            pass  # 게시 취소 수명만 검사하는 fixture

        def publish(self, candidate_sha, selected, succeeded):
            entered.set()
            assert release.wait(3)
            if fails:
                raise OSError("private command details")
            return {"status": "SUCCEEDED", "main_updated": True}

    service.repositories["demo"] = PausedRepository(source, allow_local=True)
    p = plan()
    rid = service.prepare(
        p,
        RunContext(p.run_id, project=p.project, source_sha="a" * 40, candidate_sha="b" * 40),
        source,
    )
    service.approve(rid, approver="operator")
    task = service.start(rid)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0.01)
        assert not task.done()
        with pytest.raises(RuntimeError, match="실행 중"):
            service.close()
    finally:
        release.set()
    result = await service.wait(rid)
    assert result.status is RunStatus.SUCCEEDED
    assert result.tracks["local"] is TrackStatus.DONE
    assert result.records
    saved = service.store.run(rid)["result"]
    assert saved["git"]["status"] == ("FAILED" if fails else "SUCCEEDED")
    for row in service.store.environments("demo").values():
        assert row["current"]["source_sha"] == "a" * 40
        assert row["current"]["candidate_sha"] == "b" * 40
    assert "private command details" not in json.dumps(saved)


async def test_project_settings_are_snapshotted_before_approval(rig: Any) -> None:
    service, source, calls = rig
    settings = service.save_project_settings(
        "demo",
        {
            "repo_url": "https://github.com/example/demo",
            "auto_detect": True,
            "default_targets": "onprem",
            "cloud_domain": "app.example.test",
        },
        updated_by="operator",
        expected_version=0,
    )
    p = plan()
    rid = service.prepare(p, RunContext(p.run_id, project=p.project), source)
    view = service.approval_view(rid)
    assert "watch_branch" not in view["project_settings"]
    assert view["project_settings"]["version"] == settings["version"]
    assert view["targets"] is None
    service.save_project_settings(
        "demo",
        {
            "repo_url": "https://github.com/example/next",
            "default_targets": "cloud",
            "cloud_domain": "next.example.test",
        },
        updated_by="operator",
        expected_version=settings["version"],
    )
    service.approve(rid, approver="operator")
    service.start(rid)
    assert (await service.wait(rid)).status is RunStatus.SUCCEEDED
    assert all(ctx.cloud_domain == "app.example.test" for _, ctx in calls.contexts)
    assert all(ctx.project_settings["repo_url"].endswith("/demo") for _, ctx in calls.contexts)
    assert service.get_run(rid)["context"]["targets"] is None
    assert service.get_release(rid)["result"]["tracks"]["cloud"] == "DONE"
    assert service.get_approvals(rid)[0].decision == "approved"
    assert service.list_runs()[0]["run_id"] == rid
    assert service.get_project_settings("demo")["cloud_domain"] == "next.example.test"


async def test_completed_run_public_reads_survive_restart(rig: Any) -> None:
    service, source, _calls = rig
    rid = prepare(service, source)
    view = service.approval_view(rid)
    service.approve(rid, approver="operator")
    service.start(rid)
    await service.wait(rid)
    service.close()
    reopened = DeploymentService(service.registry, service.root)
    try:
        assert reopened.get_run(rid)["status"] == "SUCCEEDED"
        assert reopened.approval_view(rid) == view
        assert reopened.get_release(rid)["release_id"] == rid
        assert reopened.get_environments("demo")["local"]["current"]["release_id"] == rid
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "data",
    [
        {"repo_url": "https://name:private@example.test/repo"},
        {"repo_url": "https://github.com/example/repo?token=private"},
        {"auto_detect": True},
        {"watch_branch": "../bad"},
        {"cloud_domain": "127.0.0.1"},
        {"default_targets": "everything"},
    ],
)
async def test_invalid_project_settings_are_rejected_without_exposing_values(rig: Any, data: Any):
    service, _source, _calls = rig
    with pytest.raises(ValueError) as error:
        service.save_project_settings("demo", data, updated_by="operator", expected_version=0)
    assert "private" not in str(error.value)
    assert service.get_project_settings("demo") is None


@pytest.mark.parametrize("omitted", ["local", "cloud"])
async def test_domain_only_settings_preserve_legacy_single_target_plan(rig: Any, omitted: str):
    service, source, _calls = rig
    service.save_project_settings(
        "demo", {"cloud_domain": "app.example.test"}, updated_by="operator", expected_version=0
    )
    p = plan()
    p = p.model_copy(
        update={"deploy": p.deploy.model_copy(update={omitted: Section()}), "verify": Section()}
    )
    rid = service.prepare(p, RunContext(p.run_id, project=p.project), source)
    assert service.approval_view(rid)["targets"] is None


async def test_settings_require_version_and_preserve_unmentioned_fields(rig: Any):
    service, _source, _calls = rig
    row = service.save_project_settings(
        "demo",
        {
            "repo_url": "https://github.com/example/demo",
            "default_targets": "onprem",
        },
        updated_by="operator",
        expected_version=0,
    )
    with pytest.raises(DdakToolError, match="버전"):
        service.save_project_settings("demo", {}, updated_by="operator", expected_version=None)
    saved = service.save_project_settings(
        "demo",
        {"cloud_domain": "app.example.test"},
        updated_by="operator",
        expected_version=row["version"],
    )
    assert saved["repo_url"] == row["repo_url"] and saved["default_targets"] == "onprem"
    with pytest.raises(DdakToolError):
        service.save_project_settings(
            "demo", {}, updated_by="operator", expected_version=row["version"]
        )


async def test_legacy_approval_without_new_export_is_readable(rig: Any):
    service, source, _calls = rig
    rid = prepare(service, source)
    before = service.approval_view(rid)
    service.approve(rid, approver="operator")
    service.start(rid)
    await service.wait(rid)
    (service.root / "runs" / rid / "approval-view.json").unlink()
    with service.store.connection() as db:
        db.execute("DELETE FROM prepared_runs WHERE run_id=?", (rid,))
    service.close()
    reopened = DeploymentService(service.registry, service.root)
    try:
        view = reopened.approval_view(rid)
        assert view["legacy_record"] is True
        for key in ("patch", "patch_meta", "infra_summary", "snapshot", "plan"):
            assert view[key] == before[key]
        assert view["unavailable_fields"] == []
    finally:
        reopened.close()


@pytest.mark.parametrize("target", ["local", "cloud", "both"])
async def test_none_targets_preserves_plan_despite_saved_default(rig, target):
    service, source, calls = rig
    service.save_project_settings(
        "demo",
        {"default_targets": "onprem" if target != "local" else "both"},
        updated_by="operator",
        expected_version=0,
    )
    p = plan()
    if target != "both":
        getattr(p.deploy, "cloud" if target == "local" else "local").steps.clear()
        p.verify.steps.clear()
    rid = service.prepare(p, RunContext(p.run_id, project=p.project), source)
    view = service.approval_view(rid)
    assert view["targets"] is None
    assert set(view["project_settings"]) == {"default_targets", "version"}
    service.approve(rid, approver="operator")
    service.start(rid)
    assert (await service.wait(rid)).status is RunStatus.SUCCEEDED
    assert {name for name, _ in calls.contexts if name.startswith("deploy.")} == (
        {"deploy.local", "deploy.cloud"} if target == "both" else {"deploy." + target}
    )


@pytest.mark.parametrize("migration_tier", [None, "db", "was"])
async def test_rollback_budget_includes_prepare_db_tiers(rig, monkeypatch, migration_tier):
    from ddak.executor.engine import Executor

    service, source, _calls = rig
    registry = Registry([*service.registry.specs, spec_for("prepare_db")])
    for name in service.registry.registered():
        registry.tool(name)(service.registry.get(name).fn)

    @registry.tool("prepare_db")
    async def prepare_db(inp: Input, ctx: RunContext) -> Output:
        raise AssertionError("budget capture precedes tool execution")

    service.registry = registry
    p = plan()
    p = p.model_copy(
        update={
            "deploy": p.deploy.model_copy(
                update={
                    "local": p.deploy.local.model_copy(
                        update={
                            "steps": [
                                step(
                                    "prepare.db.local",
                                    "prepare_db",
                                    target=Target.LOCAL,
                                    tier=migration_tier,
                                ),
                                *p.deploy.local.steps,
                            ]
                        }
                    )
                }
            )
        }
    )
    budgets = []

    def capture(self, *args, **kwargs):
        budgets.append(kwargs["rollback_timeouts"])
        raise RuntimeError("fixture: inspect budgets before any tool dispatch")

    monkeypatch.setattr(Executor, "__init__", capture)
    rid = service.prepare(p, RunContext(p.run_id, project=p.project), source)
    service.approve(rid, approver="operator")
    service.start(rid)
    assert (await service.wait(rid)).status is RunStatus.FAILED_BEFORE_DEPLOY
    count = 2 if migration_tier == "db" else 1
    per_tier = service.registry.spec("rollback_tier").timeout_s + 2
    assert budgets == [{Target.LOCAL: count * per_tier + 2, Target.CLOUD: per_tier + 2}]
