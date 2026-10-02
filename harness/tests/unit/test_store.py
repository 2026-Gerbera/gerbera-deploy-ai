"""실제 SQLite로 승인 원자성 및 프로젝트 잠금 수명을 확인한다."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from ddak.core.contracts.approval import ApprovalRecord
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.store import Store, release_view

HASH = "sha256:" + "a" * 64


def approval(run_id: str, **changes: object) -> ApprovalRecord:
    return ApprovalRecord.model_validate(
        {
            "run_id": run_id,
            "project": "demo",
            "approval_id": "one-click",
            "kind": "deploy",
            "bound_to": HASH,
            "approver": "operator",
            "approved_at": datetime.now(UTC),
            "decision": "approved",
            **changes,
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state.sqlite")


def test_one_click_rows_and_run_transition_are_atomic(store: Store) -> None:
    store.create_run("run-1", "demo", HASH)
    records = [approval("run-1"), approval("run-1", kind="infra")]
    # 두 번째 INSERT가 실패해도 첫 번째 승인과 APPROVED 상태가 남아서는 안 된다.
    with store.connection() as db:
        db.execute(
            "CREATE TRIGGER reject_infra BEFORE INSERT ON approvals "
            "WHEN NEW.kind='infra' BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        store.approve(records)
    assert store.approvals("run-1") == []
    assert store.run("run-1")["status"] == "AWAITING_APPROVAL"

    with store.connection() as db:
        db.execute("DROP TRIGGER reject_infra")
    store.approve(records)
    saved = store.approvals("run-1")
    assert {r.kind for r in saved} == {"deploy", "infra"}
    assert {r.approval_id for r in saved} == {"one-click"}
    assert store.run("run-1")["status"] == "APPROVED"


def test_concurrent_requests_for_one_project_have_one_lock_owner(store: Store) -> None:
    for run_id in ("run-1", "run-2"):
        store.create_run(run_id, "demo", HASH)
        store.approve([approval(run_id)])
    barrier = Barrier(2)

    def acquire(run_id: str) -> tuple[str, str | ErrorCode]:
        barrier.wait(timeout=5)
        try:
            return run_id, store.acquire("demo", run_id)
        except DdakToolError as exc:
            return run_id, exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(acquire, ("run-1", "run-2")))
    assert sum(value is ErrorCode.LOCK_HELD for _, value in outcomes) == 1
    winner, token = next(item for item in outcomes if item[1] is not ErrorCode.LOCK_HELD)
    loser = next(run_id for run_id, value in outcomes if value is ErrorCode.LOCK_HELD)
    assert not isinstance(token, ErrorCode)
    assert store.run(winner)["status"] == "RUNNING"
    assert store.run(loser)["status"] == "APPROVED"
    store.release("demo", loser, token)
    store.check_lock("demo", winner, token)


def test_expired_lock_survives_controller_restart(store: Store) -> None:
    for run_id in ("run-1", "run-2"):
        store.create_run(run_id, "demo", HASH)
        store.approve([approval(run_id)])
    token = store.acquire("demo", "run-1")
    with store.connection() as db:
        db.execute("UPDATE locks SET expires=0 WHERE project='demo'")

    restarted = Store(store.path)
    assert restarted.recover_interrupted() == ["run-1"]
    assert restarted.run("run-1")["status"] == "NEEDS_HUMAN"
    with pytest.raises(DdakToolError) as stale:
        restarted.check_lock("demo", "run-1", token)
    assert stale.value.code is ErrorCode.LOCK_INVALID
    with pytest.raises(DdakToolError) as refresh:
        restarted.heartbeat("demo", "run-1", token)
    assert refresh.value.code is ErrorCode.LOCK_INVALID
    with pytest.raises(DdakToolError) as blocked:
        restarted.acquire("demo", "run-2")
    assert blocked.value.code is ErrorCode.LOCK_HELD
    with restarted.connection() as db:
        assert db.execute("SELECT run_id FROM locks").fetchone()[0] == "run-1"


def test_other_run_or_project_approval_does_not_authorize_start(store: Store) -> None:
    for run_id in ("run-1", "run-2"):
        store.create_run(run_id, "demo", HASH)
    store.approve([approval("run-1")])
    with pytest.raises(DdakToolError) as missing:
        store.acquire("demo", "run-2")
    assert missing.value.code is ErrorCode.APPROVAL_REQUIRED
    with pytest.raises(DdakToolError) as foreign:
        store.approve([approval("run-2", project="other-project")])
    assert foreign.value.code is ErrorCode.PRECONDITION_FAILED
    assert store.approvals("run-2") == []
    assert store.run("run-2")["status"] == "AWAITING_APPROVAL"
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM locks").fetchone()[0] == 0


def test_heartbeat_checks_expiry_after_acquiring_sqlite_write_lock(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.create_run("run-1", "demo", HASH)
    store.approve([approval("run-1")])
    token = store.acquire("demo", "run-1")
    clock = [90.0]
    monkeypatch.setattr("ddak.core.store.time", SimpleNamespace(time=lambda: clock[0]))
    waiting = Event()
    original_connection = store.connection

    @contextmanager
    def connection() -> Iterator[sqlite3.Connection]:
        with original_connection() as db:

            def trace(sql: str) -> None:
                if sql.startswith(("BEGIN IMMEDIATE", "UPDATE locks SET")):
                    waiting.set()

            db.set_trace_callback(trace)
            yield db

    with original_connection() as blocker:
        blocker.execute("UPDATE locks SET expires=100")
        blocker.commit()
        blocker.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(store, "connection", connection)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(store.heartbeat, "demo", "run-1", token)
            try:
                assert waiting.wait(timeout=5)
                clock[0] = 101.0
            finally:
                blocker.commit()
            with pytest.raises(DdakToolError) as expired:
                pending.result(timeout=5)
            assert expired.value.code is ErrorCode.LOCK_INVALID
    with original_connection() as db:
        assert db.execute("SELECT expires FROM locks").fetchone()[0] == 100


def test_release_preserves_validated_hash_metadata_for_password_named_files(store: Store) -> None:
    release = {
        "release_id": "run-1",
        "files": {"password_reset.py": {"sha256": HASH, "executable": True}},
        "source_files": {
            "password_reset.py": {"sha256": "sha256:" + "b" * 64, "executable": False}
        },
        "password": "fixture-only",
    }
    safe = release_view(release)
    assert safe["files"] == release["files"]
    assert safe["source_files"] == release["source_files"]
    assert safe["password"] != release["password"]
    store.create_run("run-1", "demo", HASH)
    store.finish("run-1", "SUCCEEDED", {}, release, {"local": ("SUCCEEDED", release)})
    with store.connection() as db:
        manifest = json.loads(db.execute("SELECT manifest FROM releases").fetchone()[0])
    for saved in (manifest, store.environments("demo")["local"]["current"]):
        assert saved["files"] == release["files"]
        assert saved["source_files"] == release["source_files"]
        assert saved["password"] != release["password"]
    with pytest.raises(ValueError, match="해시와 실행권한"):
        release_view(
            {
                "files": {
                    "password_reset.py": {
                        "sha256": HASH,
                        "executable": False,
                        "contents": "not hash metadata",
                    }
                }
            }
        )


def test_project_settings_use_optimistic_version(store: Store) -> None:
    first = store.save_project_settings(
        "flaskr",
        {"cloud_domain": "app.example.com", "dns_mode": "external"},
        updated_by="tester",
        expected_version=0,
    )
    assert first["version"] == 1
    saved = store.project_settings("flaskr")
    assert saved is not None
    assert saved["cloud_domain"] == "app.example.com"
    with pytest.raises(DdakToolError, match="새로고침"):
        store.save_project_settings(
            "flaskr",
            {"cloud_domain": "new.example.com"},
            updated_by="tester",
            expected_version=0,
        )


def test_legacy_domain_save_merges_only_supplied_settings(tmp_path):
    store = Store(tmp_path / "settings.sqlite")
    first = store.save_project_settings(
        "demo",
        {
            "repo_url": "https://github.com/example/app",
            "watch_branch": "prod",
            "auto_detect": True,
            "default_targets": "both",
        },
        updated_by="operator",
        expected_version=0,
    )
    changed = store.save_project_settings(
        "demo",
        {"cloud_domain": "app.example.test", "dns_mode": "external", "hosted_zone_id": None},
        updated_by="legacy-web",
        expected_version=first["version"],
    )
    for key in ("repo_url", "watch_branch", "auto_detect", "default_targets"):
        assert changed[key] == first[key]
    assert changed["version"] == first["version"] + 1
    assert store.project_settings("demo") == changed
