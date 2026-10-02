"""승인 대체·만료 잠금·수동 해제의 SQLite 회귀 테스트. 외부 인프라는 사용하지 않는다."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

from ddak.core.contracts.approval import ApprovalRecord
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.registry import Registry
from ddak.core.store import Store
from ddak.executor.service import DeploymentService

HASH = "sha256:" + "a" * 64
OLD_SHA, NEW_SHA = "a" * 40, "b" * 40


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "ddak.sqlite")


def approval(run_id: str, project: str = "demo") -> ApprovalRecord:
    return ApprovalRecord(
        run_id=run_id,
        project=project,
        approval_id="one-click",
        kind="deploy",
        bound_to=HASH,
        approver="operator",
        approved_at=datetime.now(UTC),
        decision="approved",
    )


def prepared(
    store: Store,
    run_id: str,
    sha: str = OLD_SHA,
    *,
    project: str = "demo",
    trigger: str = "auto",
    ref: str = "prod",
    targets: str = "both",
) -> None:
    store.create_run(run_id, project, HASH)
    store.save_prepared(
        run_id,
        {"context": {"source_sha": sha, "trigger": trigger, "ref": ref, "targets": targets}},
    )


def approved(store: Store, run_id: str, sha: str = OLD_SHA, **kwargs: str) -> None:
    prepared(store, run_id, sha, **kwargs)
    store.approve([approval(run_id, kwargs.get("project", "demo"))])


def test_supersede_only_prior_prepared_auto_branch_with_other_sha(store: Store) -> None:
    prepared(store, "old")
    prepared(store, "same-sha", NEW_SHA)
    prepared(store, "other-project", project="other")
    prepared(store, "manual", trigger="manual")
    prepared(store, "tag", trigger="manual", ref="v1.2.3")
    prepared(store, "other-branch", ref="staging")
    approved(store, "approved")
    approved(store, "running", project="active-project")
    store.acquire("active-project", "running")
    store.create_run("not-prepared", "demo", HASH)
    prepared(store, "failed-new", NEW_SHA)
    store.preparation_failed("failed-new", "demo", {"error": "prepare failed"})
    with pytest.raises(DdakToolError):
        store.supersede_awaiting("demo", "failed-new", NEW_SHA)
    assert store.run("old")["status"] == "AWAITING_APPROVAL"
    prepared(store, "new", NEW_SHA)
    prepared(store, "later")

    assert store.supersede_awaiting("demo", "new", NEW_SHA) == ["old"]
    old = store.run("old")
    assert old["status"] == "SUPERSEDED" and old["finished"] is not None
    assert old["result"]["superseded_by"] == "new"
    assert store.prepared("old")["context"]["source_sha"] == OLD_SHA
    assert not store.preparation_failed("old", "demo", {"error": "late failure"})
    for run_id in ("same-sha", "other-project", "manual", "tag", "other-branch", "later", "new"):
        assert store.run(run_id)["status"] == "AWAITING_APPROVAL"
    assert store.run("approved")["status"] == "APPROVED"
    assert store.run("running")["status"] == "RUNNING"
    assert store.supersede_awaiting("demo", "new", NEW_SHA) == []
    assert {r["run_id"]: r["status"] for r in store.list_runs(100)}["old"] == "SUPERSEDED"
    with pytest.raises(DdakToolError, match="SUPERSEDED"):
        store.approve([approval("old")])
    with pytest.raises(DdakToolError, match="SUPERSEDED"):
        store.acquire("demo", "old")


@pytest.mark.parametrize(
    ("trigger", "ref"), [("manual", "prod"), ("manual", "v2"), ("auto", "refs/tags/v2")]
)
def test_manual_or_tag_run_does_not_supersede(store: Store, trigger: str, ref: str) -> None:
    prepared(store, "old")
    prepared(store, "new", NEW_SHA, trigger=trigger, ref=ref)
    assert store.supersede_awaiting("demo", "new", NEW_SHA) == []
    assert store.run("old")["status"] == "AWAITING_APPROVAL"


def test_supersede_rejects_unprepared_or_wrong_source_without_closing_old(store: Store) -> None:
    prepared(store, "old")
    store.create_run("new", "demo", HASH)
    with pytest.raises(DdakToolError):
        store.supersede_awaiting("demo", "new", NEW_SHA)
    store.save_prepared(
        "new", {"context": {"source_sha": NEW_SHA, "trigger": "auto", "ref": "prod"}}
    )
    for project, sha in (("other", NEW_SHA), ("demo", OLD_SHA)):
        with pytest.raises(DdakToolError):
            store.supersede_awaiting(project, "new", sha)
    assert store.run("old")["status"] == "AWAITING_APPROVAL"


def test_supersede_rolls_back_all_old_runs_on_database_failure(store: Store) -> None:
    prepared(store, "old-1")
    prepared(store, "old-2")
    prepared(store, "new", NEW_SHA)
    with store.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_supersede BEFORE UPDATE ON runs WHEN NEW.run_id='old-2' "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.supersede_awaiting("demo", "new", NEW_SHA)
    assert store.run("old-1")["status"] == store.run("old-2")["status"] == "AWAITING_APPROVAL"


def test_approval_and_supersede_race_is_serialized(store: Store) -> None:
    prepared(store, "old")
    prepared(store, "new", NEW_SHA)
    barrier = Barrier(2)

    def approve() -> bool:
        barrier.wait(timeout=5)
        try:
            store.approve([approval("old")])
            return True
        except DdakToolError as exc:
            assert "SUPERSEDED" in str(exc)
            return False

    def supersede() -> list[str]:
        barrier.wait(timeout=5)
        return store.supersede_awaiting("demo", "new", NEW_SHA)

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted, replaced = pool.submit(approve), pool.submit(supersede)
        if accepted.result(timeout=5):
            assert replaced.result(timeout=5) == []
            assert store.run("old")["status"] == "APPROVED"
        else:
            assert replaced.result(timeout=5) == ["old"]
            assert store.approvals("old") == []


@pytest.mark.parametrize("missing_lock", [False, True])
def test_running_cannot_be_stolen_or_unlocked_after_ttl(store: Store, missing_lock: bool) -> None:
    approved(store, "running")
    approved(store, "next")
    token = store.acquire("demo", "running")
    with store.connection() as db:
        db.execute("UPDATE locks SET expires=0")
        if missing_lock:
            db.execute("DELETE FROM locks")
    with pytest.raises(DdakToolError) as expired:
        store.heartbeat("demo", "running", token)
    assert expired.value.code is ErrorCode.LOCK_INVALID
    for call in (
        lambda: store.acquire("demo", "next"),
        lambda: store.unlock_project("demo", "operator", "expired"),
        lambda: store.assert_idle("demo"),
    ):
        with pytest.raises(DdakToolError) as blocked:
            call()
        assert blocked.value.code is ErrorCode.LOCK_HELD
    state = store.project_state("demo")
    assert state["active_runs"] == ["running"]
    assert token not in json.dumps(state)
    assert store.unlock_history("demo") == []
    assert store.run("running")["status"] == "RUNNING"
    assert store.run("next")["status"] == "APPROVED"


def test_expired_terminal_lock_is_reclaimed_and_old_owner_cannot_touch_new(store: Store) -> None:
    approved(store, "old")
    old_token = store.acquire("demo", "old")
    store.finish("old", "SUCCEEDED", {}, {}, {})
    approved(store, "new", NEW_SHA)
    with pytest.raises(DdakToolError) as held:
        store.acquire("demo", "new")
    assert held.value.code is ErrorCode.LOCK_HELD
    with store.connection() as db:
        db.execute("UPDATE locks SET expires=0")
    token = store.acquire("demo", "new")
    store.release("demo", "old", old_token)
    with pytest.raises(DdakToolError):
        store.heartbeat("demo", "old", old_token)
    store.check_lock("demo", "new", token)


def test_restart_recovers_selected_environment_and_removes_all_orphan_locks(store: Store) -> None:
    approved(store, "interrupted")
    store.acquire("demo", "interrupted", targets="cloud")
    with store.connection() as db:
        db.execute("INSERT INTO locks VALUES ('other', 'missing-run', 'unused', 0, 99999999999)")
    reopened = Store(store.path)
    assert reopened.recover_interrupted() == ["interrupted"]
    assert reopened.recover_interrupted() == []
    state = reopened.project_state("demo")
    assert state["lock"] is None and state["active_runs"] == []
    assert state["blocked_targets"] == ["cloud"]
    assert state["needs_human_runs"] == ["interrupted"]
    assert reopened.project_state("other")["blocked_targets"] == ["cloud", "local"]
    approved(reopened, "onprem", NEW_SHA)
    token = reopened.acquire("demo", "onprem", targets="onprem")
    reopened.check_lock("demo", "onprem", token)


@pytest.mark.parametrize("targets", [None, "both", "cloud", ["local", "cloud"]])
def test_selected_blocked_environment_cannot_start(store: Store, targets: object) -> None:
    approved(store, "interrupted", targets="cloud")
    store.acquire("demo", "interrupted", targets="cloud")
    store.recover_interrupted()
    approved(store, "next")
    with pytest.raises(DdakToolError) as blocked:
        store.acquire("demo", "next", targets=targets)
    assert blocked.value.code is ErrorCode.PRECONDITION_FAILED
    assert store.project_state("demo")["lock"] is None


@pytest.mark.parametrize("targets", ["", "unknown", [], ["local", "unknown"]])
def test_invalid_targets_cannot_bypass_recovery_guard(store: Store, targets: object) -> None:
    approved(store, "new")
    with pytest.raises(ValueError):
        store.acquire("demo", "new", targets=targets)
    assert store.run("new")["status"] == "APPROVED"


def test_manual_unlock_is_durable_audit_not_infrastructure_success(store: Store) -> None:
    prepared(store, "baseline")
    release = {"release_id": "baseline"}
    store.finish("baseline", "SUCCEEDED", {}, release, {"cloud": ("SUCCEEDED", release)})
    approved(store, "interrupted", targets="cloud")
    store.acquire("demo", "interrupted", targets="cloud")
    store.mark_stopped("interrupted", "NEEDS_HUMAN")
    before = store.environments("demo")
    audit = store.unlock_project("demo", "operator", "checked manually; retry allowed")
    assert audit["created"] > 0 and audit["actor"] == "operator"
    assert audit["released_lock"] == "interrupted"
    assert audit["cleared_targets"] == ["cloud"]
    assert audit["unlocked"] is True and audit["recovery_verified"] is False
    assert store.environments("demo") == before
    assert store.run("interrupted")["status"] == "NEEDS_HUMAN"
    assert store.release_record("interrupted") is None
    reopened = Store(store.path)
    reopened.recover_interrupted()
    assert reopened.unlock_history("demo") == [audit]
    assert reopened.unlock_history("other") == []
    assert reopened.project_state("demo")["blocked_targets"] == []
    reopened.assert_idle("demo")
    approved(reopened, "next", NEW_SHA)
    reopened.acquire("demo", "next", targets="cloud")
    # 한 번의 수동 확인으로 미래의 실패까지 면제하지 않는다.
    reopened.mark_stopped("next", "NEEDS_HUMAN")
    assert reopened.project_state("demo")["blocked_targets"] == ["cloud"]


def test_unlock_and_audit_are_atomic_on_database_failure(store: Store) -> None:
    approved(store, "old", targets="cloud")
    store.acquire("demo", "old", targets="cloud")
    store.mark_stopped("old", "NEEDS_HUMAN")
    before = store.project_state("demo")
    with store.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON unlock_audit "
            "BEGIN SELECT RAISE(ABORT, 'audit failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="audit failed"):
        store.unlock_project("demo", "operator", "retry")
    assert store.project_state("demo") == before
    assert store.unlock_history("demo") == []


@pytest.mark.parametrize(("actor", "reason"), [("", "checked"), ("operator", " ")])
def test_unlock_requires_actor_and_reason(store: Store, actor: str, reason: str) -> None:
    with pytest.raises(ValueError):
        store.unlock_project("demo", actor, reason)
    assert store.unlock_history("demo") == []


def test_new_finish_revokes_unlock_only_for_changed_environment(store: Store) -> None:
    with store.connection() as db:
        for target in ("local", "cloud"):
            db.execute(
                "INSERT INTO env_release VALUES ('demo', ?, 'NEEDS_HUMAN', NULL, NULL)", (target,)
            )
    store.unlock_project("demo", "operator", "allow retries")
    approved(store, "new")
    store.acquire("demo", "new", targets="cloud")
    store.finish("new", "NEEDS_HUMAN", {}, {}, {"cloud": ("NEEDS_HUMAN", None)})
    assert store.project_state("demo")["blocked_targets"] == ["cloud"]
    approved(store, "local-next")
    token = store.acquire("demo", "local-next", targets="local")
    store.check_lock("demo", "local-next", token)
    assert store.environments("demo")["local"]["status"] == "NEEDS_HUMAN"


def test_live_controller_lease_rejects_another_process_even_after_ttl(store: Store) -> None:
    controller = DeploymentService(Registry(()), store.path.parent)
    try:
        approved(store, "running")
        store.acquire("demo", "running", targets="cloud")
        with store.connection() as db:
            db.execute("UPDATE locks SET expires=0")
        # 두 번째 프로세스는 recover_interrupted 진입 전에 flock에서 거부되어야 한다.
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path\n"
                "import sys\n"
                "from ddak.core.contracts.errors import DdakToolError, ErrorCode\n"
                "from ddak.core.registry import Registry\n"
                "from ddak.executor.service import DeploymentService\n"
                "try:\n"
                "    service = DeploymentService(Registry(()), Path(sys.argv[1]))\n"
                "except DdakToolError as exc:\n"
                "    sys.exit(0 if exc.code is ErrorCode.LOCK_HELD else 2)\n"
                "else:\n"
                "    service.close()\n"
                "    sys.exit(1)\n",
                str(store.path.parent),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert probe.returncode == 0, probe.stderr
        assert store.run("running")["status"] == "RUNNING"
        assert store.project_state("demo")["lock"]["expired"] is True
    finally:
        controller.close()
    restarted = DeploymentService(Registry(()), store.path.parent)
    try:
        assert restarted.interrupted == ["running"]
        assert store.project_state("demo")["lock"] is None
        assert store.project_state("demo")["blocked_targets"] == ["cloud"]
    finally:
        restarted.close()


def test_unlock_cannot_remove_a_new_lock_during_concurrent_start(store: Store) -> None:
    approved(store, "old")
    store.acquire("demo", "old", targets="cloud")
    store.recover_interrupted()
    store.unlock_project("demo", "operator", "allow retry")
    approved(store, "new")
    barrier = Barrier(2)

    def unlock() -> None:
        barrier.wait(timeout=5)
        try:
            store.unlock_project("demo", "operator", "second click")
        except DdakToolError as exc:
            assert exc.code is ErrorCode.LOCK_HELD

    def acquire() -> str:
        barrier.wait(timeout=5)
        return store.acquire("demo", "new", targets="cloud")

    with ThreadPoolExecutor(max_workers=2) as pool:
        unlocked, started = pool.submit(unlock), pool.submit(acquire)
        unlocked.result(timeout=5)
        token = started.result(timeout=5)
    store.check_lock("demo", "new", token)
    assert store.project_state("demo")["active_runs"] == ["new"]


def test_unknown_orphan_unlock_preserves_unknown_state_and_does_not_clear_diverged(
    store: Store,
) -> None:
    with store.connection() as db:
        db.execute("INSERT INTO locks VALUES ('demo', 'missing', 'unused', 0, 99999999999)")
    audit = store.unlock_project("demo", "operator", "acknowledge unknown owner")
    assert audit["cleared_targets"] == ["cloud", "local"]
    assert store.project_state("demo")["blocked_targets"] == []
    assert all(row["status"] == "NEEDS_HUMAN" for row in store.environments("demo").values())
    with store.connection() as db:
        db.execute("UPDATE env_release SET status='DIVERGED' WHERE target='cloud'")
    store.unlock_project("demo", "operator", "repeat acknowledgement")
    assert store.project_state("demo")["blocked_targets"] == ["cloud"]
