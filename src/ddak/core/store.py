"""단일 컨트롤러용 SQLite 실행 장부. 만료 잠금도 자동 탈취하지 않는다."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ddak.core.contracts.approval import ApprovalRecord
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.redact import redact_obj

_DDL = """
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, project TEXT NOT NULL, status TEXT NOT NULL,
 plan_hash TEXT NOT NULL, created REAL NOT NULL, finished REAL, result TEXT);
CREATE TABLE IF NOT EXISTS approvals (
 run_id TEXT NOT NULL, kind TEXT NOT NULL, record TEXT NOT NULL,
 PRIMARY KEY (run_id, kind));
CREATE TABLE IF NOT EXISTS locks (
 project TEXT PRIMARY KEY, run_id TEXT NOT NULL, token TEXT NOT NULL,
 heartbeat REAL NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS steps (
 run_id TEXT NOT NULL, step_id TEXT NOT NULL, record TEXT NOT NULL,
 PRIMARY KEY (run_id, step_id));
CREATE TABLE IF NOT EXISTS releases (
 run_id TEXT PRIMARY KEY, manifest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS env_release (
 project TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
 current TEXT, previous TEXT, PRIMARY KEY (project, target));
CREATE TABLE IF NOT EXISTS project_settings (
 project TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL,
 updated_by TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS prepared_runs (
 run_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
"""


def _json(value: Any) -> str:
    return json.dumps(redact_obj(value), ensure_ascii=False, sort_keys=True)


def release_view(value: dict[str, Any]) -> dict[str, Any]:
    """파일명에 password 등이 있어도 비밀값 없는 해시 장부는 그대로 보존한다."""
    safe = redact_obj(value)
    for key in ("files", "source_files"):
        if key not in value:
            continue
        files = value[key]
        if not isinstance(files, dict):
            raise ValueError("파일 해시 목록 형식 오류")
        for metadata in files.values():
            if (
                not isinstance(metadata, dict)
                or set(metadata) != {"sha256", "executable"}
                or not isinstance(metadata["sha256"], str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", metadata["sha256"])
                or not isinstance(metadata["executable"], bool)
            ):
                raise ValueError("파일 목록에는 해시와 실행권한만 저장한다")
        safe[key] = files
    return safe


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(_DDL)
        path.chmod(0o600)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_run(self, run_id: str, project: str, plan_hash: str) -> None:
        with self.connection() as db:
            try:
                db.execute(
                    "INSERT INTO runs VALUES (?, ?, 'AWAITING_APPROVAL', ?, ?, NULL, NULL)",
                    (run_id, project, plan_hash, time.time()),
                )
            except sqlite3.IntegrityError:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "run_id는 재사용할 수 없다"
                ) from None

    def run(self, run_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        result = dict(row)
        if result["result"]:
            result["result"] = json.loads(result["result"])
        return result

    def preparation_failed(self, run_id: str, project: str, result: dict[str, Any]) -> bool:
        """계획/prepare 실패도 목록에 남긴다. 이미 승인·실행한 run은 덮어쓰지 않는다."""
        with self.connection() as db:
            cursor = db.execute(
                "INSERT INTO runs VALUES (?, ?, 'FAILED_BEFORE_DEPLOY', '', ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET status='FAILED_BEFORE_DEPLOY', "
                "finished=excluded.finished, result=excluded.result "
                "WHERE runs.status='AWAITING_APPROVAL' AND runs.project=excluded.project",
                (run_id, project, time.time(), time.time(), _json(result)),
            )
            return cursor.rowcount == 1

    def save_prepared(self, run_id: str, payload: dict[str, Any]) -> None:
        # 실행 입력의 해시가 바뀌면 안 된다. 표시용 redact 데이터와 구분해 0600 DB에 보관한다.
        with self.connection() as db:
            db.execute(
                "INSERT INTO prepared_runs VALUES (?, ?)",
                (run_id, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            )

    def prepared(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM prepared_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """관리 화면용 최근 실행 목록. 저장된 결과는 이미 redact된 값이다."""
        safe_limit = max(1, min(limit, 100))
        with self.connection() as db:
            rows = db.execute(
                "SELECT run_id, project, status, created, finished FROM runs "
                "ORDER BY created DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def project_settings(self, project: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT version, data, updated_by, updated_at FROM project_settings "
                "WHERE project=?",
                (project,),
            ).fetchone()
        if row is None:
            return None
        return {
            "project": project,
            "version": row["version"],
            **json.loads(row["data"]),
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
        }

    def save_project_settings(
        self,
        project: str,
        data: dict[str, Any],
        *,
        updated_by: str,
        expected_version: int | None,
    ) -> dict[str, Any]:
        """낙관적 버전 검사로 도메인 설정 덮어쓰기를 막는다."""
        safe_data = _json(data)
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT version, data FROM project_settings WHERE project=?", (project,)
            ).fetchone()
            current = row["version"] if row else 0
            if expected_version is not None and current != expected_version:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "설정이 다른 화면에서 변경됐다. 새로고침하세요"
                )
            safe_data = _json({**(json.loads(row["data"]) if row else {}), **json.loads(safe_data)})
            version = current + 1
            db.execute(
                "INSERT OR REPLACE INTO project_settings VALUES (?, ?, ?, ?, ?)",
                (project, version, safe_data, updated_by, now),
            )
        return {
            "project": project,
            "version": version,
            **json.loads(safe_data),
            "updated_by": updated_by,
            "updated_at": now,
        }

    def approve(self, records: Sequence[ApprovalRecord]) -> None:
        if not records:
            raise ValueError("승인 대상이 없다")
        first = records[0]
        if any(
            (r.run_id, r.project, r.approval_id, r.approver, r.decision)
            != (first.run_id, first.project, first.approval_id, first.approver, first.decision)
            for r in records
        ):
            raise ValueError("한 승인 묶음의 식별자가 다르다")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE run_id=?", (first.run_id,)).fetchone()
            if (
                run is None
                or run["project"] != first.project
                or run["status"] not in {"AWAITING_APPROVAL", "APPROVED"}
            ):
                raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "승인 가능한 실행이 아니다")
            for record in records:
                db.execute(
                    "INSERT OR REPLACE INTO approvals VALUES (?, ?, ?)",
                    (record.run_id, record.kind, record.model_dump_json()),
                )
            status = "APPROVED" if first.decision == "approved" else "FAILED_BEFORE_DEPLOY"
            db.execute("UPDATE runs SET status=? WHERE run_id=?", (status, first.run_id))

    def approvals(self, run_id: str) -> list[ApprovalRecord]:
        with self.connection() as db:
            return [
                ApprovalRecord.model_validate_json(r[0])
                for r in db.execute("SELECT record FROM approvals WHERE run_id=?", (run_id,))
            ]

    def acquire(self, project: str, run_id: str) -> str:
        token = secrets.token_urlsafe(24)
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            blocked = db.execute(
                "SELECT 1 FROM env_release WHERE project=? "
                "AND status IN ('DIVERGED','NEEDS_HUMAN')",
                (project,),
            ).fetchone()
            if blocked:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "환경 상태를 사람이 확인해야 한다"
                )
            try:
                db.execute(
                    "INSERT INTO locks VALUES (?, ?, ?, ?, ?)",
                    (project, run_id, token, now, now + 600),
                )
            except sqlite3.IntegrityError:
                raise DdakToolError(
                    ErrorCode.LOCK_HELD, "프로젝트가 실행 중이거나 복구 확인을 기다린다"
                ) from None
            cursor = db.execute(
                "UPDATE runs SET status='RUNNING' WHERE run_id=? "
                "AND project=? AND status='APPROVED'",
                (run_id, project),
            )
            if cursor.rowcount != 1:
                raise DdakToolError(ErrorCode.APPROVAL_REQUIRED, "승인된 새 실행만 시작할 수 있다")
        return token

    def check_lock(self, project: str, run_id: str, token: str | None) -> None:
        with self.connection() as db:
            row = db.execute(
                "SELECT 1 FROM locks WHERE project=? AND run_id=? AND token=? AND expires>?",
                (project, run_id, token, time.time()),
            ).fetchone()
        if row is None:
            raise DdakToolError(ErrorCode.LOCK_INVALID, "실행 잠금이 유효하지 않다")

    def heartbeat(self, project: str, run_id: str, token: str) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            cursor = db.execute(
                "UPDATE locks SET heartbeat=?, expires=? "
                "WHERE project=? AND run_id=? AND token=? AND expires>?",
                (now, now + 600, project, run_id, token, now),
            )
            if cursor.rowcount != 1:
                raise DdakToolError(ErrorCode.LOCK_INVALID, "실행 잠금이 유효하지 않다")

    def release(self, project: str, run_id: str, token: str) -> None:
        with self.connection() as db:
            db.execute(
                "DELETE FROM locks WHERE project=? AND run_id=? AND token=?",
                (project, run_id, token),
            )

    def environments(self, project: str) -> dict[str, dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM env_release WHERE project=?", (project,)).fetchall()
        return {
            r["target"]: {
                "status": r["status"],
                "current": json.loads(r["current"]) if r["current"] else None,
                "previous": json.loads(r["previous"]) if r["previous"] else None,
            }
            for r in rows
        }

    def finish(
        self,
        run_id: str,
        status: str,
        result: dict[str, Any],
        manifest: dict[str, Any],
        environments: dict[str, tuple[str, dict[str, Any] | None]],
    ) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT project FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            db.execute(
                "INSERT INTO releases VALUES (?, ?)",
                (run_id, json.dumps(release_view(manifest), ensure_ascii=False)),
            )
            for sid, record in result.get("steps", {}).items():
                db.execute(
                    "INSERT OR REPLACE INTO steps VALUES (?, ?, ?)", (run_id, sid, _json(record))
                )
            for target, (env_status, release) in environments.items():
                old = db.execute(
                    "SELECT current, previous FROM env_release WHERE project=? AND target=?",
                    (run[0], target),
                ).fetchone()
                current = old[0] if old else None
                previous = old[1] if old else None
                if release is not None:
                    previous, current = (
                        current,
                        json.dumps(release_view(release), ensure_ascii=False),
                    )
                db.execute(
                    "INSERT OR REPLACE INTO env_release VALUES (?, ?, ?, ?, ?)",
                    (run[0], target, env_status, current, previous),
                )
            db.execute(
                "UPDATE runs SET status=?, finished=?, result=? WHERE run_id=?",
                (status, time.time(), _json(result), run_id),
            )

    def release_record(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute("SELECT manifest FROM releases WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def mark_stopped(self, run_id: str, status: str) -> None:
        if status not in {"CANCELLED", "NEEDS_HUMAN"}:
            raise ValueError("허용되지 않는 중지 상태")
        with self.connection() as db:
            db.execute(
                "UPDATE runs SET status=?, finished=? WHERE run_id=?", (status, time.time(), run_id)
            )

    def recover_interrupted(self) -> list[str]:
        # 컨트롤러 독점 잠금 획득 뒤 기동할 때만 호출한다. 잠금은 자동 해제하지 않는다.
        with self.connection() as db:
            rows = db.execute("SELECT run_id FROM runs WHERE status='RUNNING'").fetchall()
            db.execute(
                "UPDATE runs SET status='NEEDS_HUMAN', finished=? WHERE status='RUNNING'",
                (time.time(),),
            )
            db.execute(
                "UPDATE runs SET status='CANCELLED', finished=? "
                "WHERE status IN ('AWAITING_APPROVAL','APPROVED') "
                "AND run_id NOT IN (SELECT run_id FROM prepared_runs)",
                (time.time(),),
            )
        return [r[0] for r in rows]
