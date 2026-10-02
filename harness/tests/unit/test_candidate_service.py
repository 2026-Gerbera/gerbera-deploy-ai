"""승인→후보 SHA→빌드 컨텍스트→환경 태그의 로컬 연결 검증."""

import pytest

from ddak.core.contracts.context import RunContext
from ddak.executor.engine import RunStatus
from tests.unit import test_deployment_service as service_tests
from tests.unit.core import test_app_repository as git_tests
from tests.unit.core import test_candidate as candidate_tests

rig = service_tests.rig
repository = git_tests.repository
operator_identity = candidate_tests.operator_identity


@pytest.mark.anyio
async def test_candidate_commit_reaches_build_and_actual_local_git_records(
    rig, repository, operator_identity
):
    service, source, calls = rig
    repo, bare, v1, v2 = repository
    repo.secret_scan = lambda path: None
    service.repositories["demo"] = repo
    (source / "app.py").write_text("version = 1\n")
    p = service_tests.plan().model_copy(update={"toggles": {"code_patch": True}})
    rid = service.prepare(
        p,
        RunContext(p.run_id, project=p.project, source_sha=v1, toggles={"code_patch": True}),
        source,
        patch=candidate_tests.PATCH,
    )
    service.approve(rid, approver="operator")
    service.start(rid)
    result = await service.wait(rid)
    assert result.status is RunStatus.SUCCEEDED
    candidate = result.context.candidate_sha
    assert candidate != v2
    assert all(ctx.candidate_sha == candidate for _, ctx in calls.contexts)
    assert git_tests.git(bare, "rev-parse", "main") == candidate
    assert git_tests.git(bare, "rev-parse", "refs/tags/deployed/onprem") == candidate
    assert service.deployment_baselines("demo") == {"local": v1, "cloud": v1}
    assert (source / "app.py").read_text() == "version = 1\n"


@pytest.mark.anyio
async def test_candidate_cancellation_stops_next_commit_or_push(rig, repository, operator_identity):
    import asyncio
    import threading

    service, source, calls = rig
    repo, bare, _v1, v2 = repository
    source_sha = candidate_tests.advance_prod(repo)
    (source / "app.py").write_text("version = 1\n")
    (source / "README").write_text("New feature\n")
    entered, release = threading.Event(), threading.Event()

    def scan(path):
        entered.set()
        assert release.wait(3)

    repo.secret_scan = lambda _: None  # source=fixture: 승인 전 검사는 결정적으로 통과
    service.repositories["demo"] = repo
    p = service_tests.plan().model_copy(update={"toggles": {"code_patch": True}})
    rid = service.prepare(
        p,
        RunContext(
            p.run_id, project=p.project, source_sha=source_sha, toggles={"code_patch": True}
        ),
        source,
        patch=candidate_tests.PATCH,
    )
    # 승인 전 검사는 통과시키고 후보 생성 중의 검사만 정지시킨다.
    repo.secret_scan = scan
    service.approve(rid, approver="operator")
    task = service.start(rid)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        release.set()
    result = await service.wait(rid)
    assert result.status is RunStatus.CANCELLED
    assert calls.contexts == []
    assert git_tests.git(bare, "rev-parse", "ai-prod") == v2


@pytest.mark.anyio
async def test_cancel_during_completed_push_keeps_candidate_sha(
    rig, repository, operator_identity, monkeypatch
):
    import asyncio
    import json
    import threading

    from ddak.core.app_repository import AppRepository

    service, source, calls = rig
    repo, bare, _v1, v2 = repository
    source_sha = candidate_tests.advance_prod(repo)
    (source / "app.py").write_text("version = 1\n")
    (source / "README").write_text("New feature\n")
    repo.secret_scan = lambda path: None
    service.repositories["demo"] = repo
    entered, release = threading.Event(), threading.Event()
    run = AppRepository.git

    def pause(self, *args, **kwargs):
        out = run(self, *args, **kwargs)
        if args[0] == "push":
            entered.set()
            assert release.wait(3)
        return out

    monkeypatch.setattr(AppRepository, "git", pause)
    p = service_tests.plan().model_copy(update={"toggles": {"code_patch": True}})
    rid = service.prepare(
        p,
        RunContext(
            p.run_id, project=p.project, source_sha=source_sha, toggles={"code_patch": True}
        ),
        source,
        patch=candidate_tests.PATCH,
    )
    service.approve(rid, approver="operator")
    task = service.start(rid)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
    finally:
        release.set()
    assert (await service.wait(rid)).status is RunStatus.CANCELLED
    assert calls.contexts == []
    candidate = json.loads((service.root / "runs" / rid / "candidate.json").read_text())
    assert candidate["candidate_sha"] == git_tests.git(bare, "rev-parse", "ai-prod") != v2


@pytest.mark.anyio
async def test_parity_without_cloud_app_changes_preserves_cloud_and_main_refs(
    rig, repository, operator_identity
):
    from ddak.executor.engine import TrackStatus

    service, source, calls = rig
    repo, bare, v1, _v2 = repository
    repo.secret_scan = lambda path: None
    service.repositories["demo"] = repo
    calls.parity_ok = False
    (source / "app.py").write_text("version = 1\n")
    p = service_tests.plan(patch=True)
    p = p.model_copy(
        update={
            "deploy": p.deploy.model_copy(
                update={
                    "cloud": p.deploy.cloud.model_copy(
                        update={
                            "steps": [s for s in p.deploy.cloud.steps if s.tool != "deploy_tier"]
                        }
                    )
                }
            )
        }
    )
    rid = service.prepare(
        p,
        RunContext(p.run_id, project=p.project, source_sha=v1, toggles={"code_patch": True}),
        source,
        patch=candidate_tests.PATCH,
    )
    service.approve(rid, approver="operator")
    service.start(rid)
    result = await service.wait(rid)
    candidate = result.context.candidate_sha
    assert result.status is RunStatus.PARITY_FAILED
    assert result.tracks["cloud"] is TrackStatus.FAILED
    assert result.tracks["local"] is TrackStatus.DONE
    assert git_tests.git(bare, "rev-parse", "main") == v1
    assert git_tests.git(bare, "rev-parse", "refs/tags/deployed/cloud") == v1
    assert git_tests.git(bare, "rev-parse", "refs/tags/deployed/onprem") == candidate
    assert "cloud" not in service.get_environments("demo")
    assert not any(name == "rollback.cloud" for name, _ in calls.contexts)


@pytest.mark.anyio
async def test_merge_conflicts_are_available_in_sealed_release(rig, repository, operator_identity):
    service, source, _calls = rig
    repo, _bare, _v1, _v2 = repository
    git_tests.git(repo.path, "switch", "-c", "prod", "origin/prod")
    (repo.path / "app.py").write_text("version = 3\n")
    git_tests.git(repo.path, "add", "app.py")
    git_tests.git(repo.path, "commit", "-m", "New product value")
    git_tests.git(repo.path, "push", "origin", "prod")
    source_sha = git_tests.git(repo.path, "rev-parse", "HEAD")
    repo.secret_scan = lambda path: None
    service.repositories["demo"] = repo
    (source / "app.py").write_text("version = 3\n")
    p = service_tests.plan(patch=True)
    patch = b"--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-version = 3\n+version = 4\n"
    rid = service.prepare(
        p,
        RunContext(
            p.run_id, project=p.project, source_sha=source_sha, toggles={"code_patch": True}
        ),
        source,
        patch=patch,
    )
    service.approve(rid, approver="operator")
    service.start(rid)
    assert (await service.wait(rid)).status is RunStatus.SUCCEEDED
    release = service.get_release(rid)
    assert release["merge_conflicts"] == ["app.py"]
    assert release["result"]["merge_conflicts"] == ["app.py"]
    service.close()
    reopened = service_tests.DeploymentService(service.registry, service.root)
    try:
        assert reopened.get_release(rid)["merge_conflicts"] == ["app.py"]
    finally:
        reopened.close()


@pytest.mark.anyio
async def test_comparison_error_keeps_deployments_but_does_not_advance_main(
    rig,
    repository,
    operator_identity,
):
    from ddak.core.contracts.errors import DdakToolError, ErrorCode
    from ddak.core.registry import Registry
    from ddak.executor.engine import TrackStatus

    service, source, calls = rig
    repo, bare, v1, _ = repository
    repo.secret_scan = lambda path: None
    service.repositories["demo"] = repo
    (source / "app.py").write_text("version = 1\n")
    registry = Registry(service.registry.specs)
    for name in service.registry.registered() - {"compare_env_results"}:
        registry.tool(name)(service.registry.get(name).fn)

    @registry.tool("compare_env_results")
    async def compare(inp: service_tests.Input, ctx: RunContext) -> service_tests.Output:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "fixture comparison unavailable")

    service.registry = registry
    p = service_tests.plan()
    rid = service.prepare(p, RunContext(p.run_id, project=p.project, source_sha=v1), source)
    service.approve(rid, approver="operator")
    service.start(rid)
    result = await service.wait(rid)
    assert result.status is RunStatus.FAILED_VERIFY
    assert result.tracks["local"] is result.tracks["cloud"] is TrackStatus.DONE
    assert not any(name.startswith("rollback") for name, _ in calls.contexts)
    assert git_tests.git(bare, "rev-parse", "main") == v1
    for target in ("onprem", "cloud"):
        assert (
            git_tests.git(bare, "rev-parse", "refs/tags/deployed/" + target)
            == result.context.candidate_sha
        )
    assert service.get_release(rid)["git"]["main_skip_reason"] == "verification_failed"
