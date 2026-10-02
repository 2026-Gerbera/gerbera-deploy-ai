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

    repo.secret_scan = scan
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
