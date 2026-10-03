"""단일 컨트롤러용 SQLite 실행 장부. 실행 중인 잠금은 만료돼도 탈취하지 않는다."""

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
from ddak.core.contracts.infra_outputs import checked_cloud_outputs
from ddak.core.project_settings import watch_source
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
CREATE TABLE IF NOT EXISTS unlock_audit (
 id INTEGER PRIMARY KEY, project TEXT NOT NULL, actor TEXT NOT NULL,
 reason TEXT NOT NULL, created REAL NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS unlock_ack (
 project TEXT NOT NULL, target TEXT NOT NULL, PRIMARY KEY (project, target));
CREATE TABLE IF NOT EXISTS run_targets (
 run_id TEXT PRIMARY KEY, targets TEXT NOT NULL);
"""


def _json(value: Any) -> str:
    return json.dumps(redact_obj(value), ensure_ascii=False, sort_keys=True)


def _targets(value: str | Sequence[str] | None) -> tuple[str, ...]:
    if value is None or value == "both":
        return ("local", "cloud")
    values = (value,) if isinstance(value, str) else tuple(value)
    chosen = {"local" if target == "onprem" else target for target in values}
    if not chosen or not chosen.issubset({"local", "cloud"}):
        raise ValueError("targets는 local/onprem/cloud/both만 허용한다")
    return tuple(sorted(chosen))


def _auto_source(payload: dict[str, Any]) -> tuple[str, str, str | None] | None:
    context = payload.get("context", {})
    sha, ref = context.get("source_sha"), context.get("ref")
    watch = context.get("project_settings", {}).get("watch_branch", "prod")
    if (
        context.get("trigger") != "auto"
        or not isinstance(sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha)
        or not isinstance(ref, str)
        or not ref
        or ref.startswith("refs/tags/")
        or ref != watch
    ):
        return None
    return sha, ref, context.get("repo_url")


def release_view(value: dict[str, Any]) -> dict[str, Any]:
    """파일명에 password 등이 있어도 비밀값 없는 해시 장부는 그대로 보존한다."""
    safe = redact_obj(value)
    if "platform_outputs" in value:
        # 허용 목록의 비민감 리소스 식별자는 다음 실행의 입력이다. ARN을 마스킹하지 않는다.
        safe["platform_outputs"] = checked_cloud_outputs(value["platform_outputs"])
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
        path = path.expanduser().resolve()
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

    def supersede_awaiting(self, project: str, run_id: str, source_sha: str) -> list[str]:
        """준비 파일까지 성공한 뒤 호출한다. 수동·태그 요청과 승인된 run은 유지한다."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT runs.rowid AS sequence, runs.status, prepared_runs.payload "
                "FROM runs JOIN prepared_runs USING(run_id) WHERE run_id=? AND project=?",
                (run_id, project),
            ).fetchone()
            if current is None or current["status"] != "AWAITING_APPROVAL":
                raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "준비된 새 승인 대기가 필요하다")
            payload = json.loads(current["payload"])
            if payload.get("context", {}).get("source_sha") != source_sha or not source_sha:
                raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "준비된 소스 SHA가 다르다")
            source = _auto_source(payload)
            if source is None:
                return []
            rows = db.execute(
                "SELECT run_id, payload FROM runs JOIN prepared_runs USING(run_id) "
                "WHERE project=? AND status='AWAITING_APPROVAL' AND runs.rowid<? "
                "ORDER BY runs.rowid",
                (project, current["sequence"]),
            ).fetchall()
            superseded = []
            now = time.time()
            for row in rows:
                previous = _auto_source(json.loads(row["payload"]))
                if previous is None or previous[0] == source[0] or previous[1:] != source[1:]:
                    continue
                db.execute(
                    "UPDATE runs SET status='SUPERSEDED', finished=?, result=? WHERE run_id=?",
                    (
                        now,
                        _json({"superseded_by": run_id, "source_sha": source_sha}),
                        row["run_id"],
                    ),
                )
                superseded.append(row["run_id"])
        return superseded

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

    def list_project_settings(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            projects = [
                row[0]
                for row in db.execute("SELECT project FROM project_settings ORDER BY project")
            ]
        return [
            settings
            for project in projects
            if (settings := self.project_settings(project)) is not None
        ]

    def platform_outputs(self, project: str, source_mode: str) -> dict[str, Any]:
        """마지막 기록된 허용 출력. FAKE 결과를 REAL 준비에 재사용하지 않는다."""
        with self.connection() as db:
            rows = db.execute(
                "SELECT releases.manifest FROM releases JOIN runs USING(run_id) "
                "WHERE runs.project=? ORDER BY runs.finished DESC",
                (project,),
            ).fetchall()
        for row in rows:
            record = json.loads(row[0])
            if record.get("source_mode") == source_mode and record.get("platform_outputs"):
                return record["platform_outputs"]
        return {}

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
            merged = {**(json.loads(row["data"]) if row else {}), **json.loads(safe_data)}
            if merged.get("auto_detect") and merged.get("repo_url"):
                identity = watch_source(merged["repo_url"], merged.get("watch_branch", "prod"))
                others = db.execute(
                    "SELECT project, data FROM project_settings "
                    "WHERE project != ? ORDER BY project",
                    (project,),
                ).fetchall()
                for other in others:
                    settings = json.loads(other["data"])
                    if not settings.get("auto_detect") or not settings.get("repo_url"):
                        continue
                    try:
                        other_source = watch_source(
                            settings["repo_url"], settings.get("watch_branch", "prod")
                        )
                    except ValueError:
                        continue  # 기존 잘못된 URL은 감시 조립에서 제외하고 경고한다.
                    if other_source == identity:
                        raise DdakToolError(
                            ErrorCode.CONFIG_INVALID,
                            f"자동 감시 중복: {other['project']} 프로젝트가 같은 저장소·브랜치를 "
                            "이미 감시합니다. 기존 프로젝트의 자동 감지를 먼저 끄세요",
                        )
            safe_data = _json(merged)
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
            if run is not None and run["status"] == "SUPERSEDED":
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "SUPERSEDED: 새 소스로 대체된 실행이다"
                )
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

    @staticmethod
    def _run_targets(db: sqlite3.Connection, run_id: str) -> tuple[str, ...]:
        chosen = db.execute("SELECT targets FROM run_targets WHERE run_id=?", (run_id,)).fetchone()
        if chosen is not None:
            return _targets(json.loads(chosen[0]))
        row = db.execute("SELECT payload FROM prepared_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return _targets(None)
        payload = json.loads(row[0])
        chosen = payload.get("context", {}).get("targets")
        if chosen is not None:
            return _targets(chosen)
        deploy = payload.get("plan", {}).get("deploy", {})
        return tuple(t for t in ("local", "cloud") if deploy.get(t, {}).get("steps")) or _targets(
            None
        )

    @staticmethod
    def _needs_human(db: sqlite3.Connection, project: str, targets: Sequence[str]) -> None:
        for target in targets:
            db.execute("DELETE FROM unlock_ack WHERE project=? AND target=?", (project, target))
            db.execute(
                "INSERT INTO env_release(project, target, status) VALUES (?, ?, 'NEEDS_HUMAN') "
                "ON CONFLICT(project,target) DO UPDATE SET status='NEEDS_HUMAN'",
                (project, target),
            )

    def _prune_locks(self, db: sqlite3.Connection, project: str) -> None:
        """TTL은 종료된 소유자에만 적용한다. RUNNING은 별도 controller lease가 보호한다."""
        row = db.execute(
            "SELECT locks.run_id, locks.expires, runs.status FROM locks "
            "LEFT JOIN runs ON runs.run_id=locks.run_id WHERE locks.project=?",
            (project,),
        ).fetchone()
        if row is None or row["status"] == "RUNNING":
            return
        if row["status"] == "NEEDS_HUMAN":
            # finish는 환경별 결과를 이미 남겼다. 그 이전 중단은 선택한 환경 모두 불명이다.
            if not db.execute("SELECT 1 FROM releases WHERE run_id=?", (row["run_id"],)).fetchone():
                self._needs_human(db, project, self._run_targets(db, row["run_id"]))
        elif row["expires"] > time.time():
            return
        elif row["status"] is None:
            self._needs_human(db, project, _targets(None))
        db.execute("DELETE FROM locks WHERE project=?", (project,))

    @staticmethod
    def _blocked_targets(db: sqlite3.Connection, project: str) -> list[str]:
        return [
            row[0]
            for row in db.execute(
                "SELECT target FROM env_release WHERE project=? AND (status='DIVERGED' "
                "OR (status='NEEDS_HUMAN' AND NOT EXISTS "
                "(SELECT 1 FROM unlock_ack WHERE unlock_ack.project=env_release.project "
                "AND unlock_ack.target=env_release.target))) ORDER BY target",
                (project,),
            )
        ]

    def acquire(
        self, project: str, run_id: str, *, targets: str | Sequence[str] | None = None
    ) -> str:
        selected = _targets(targets)
        token = secrets.token_urlsafe(24)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute(
                "SELECT status FROM runs WHERE project=? AND run_id=?", (project, run_id)
            ).fetchone()
            if run is not None and run[0] == "SUPERSEDED":
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "SUPERSEDED: 새 소스로 대체된 실행이다"
                )
            if db.execute(
                "SELECT 1 FROM runs WHERE project=? AND status='RUNNING'", (project,)
            ).fetchone():
                raise DdakToolError(ErrorCode.LOCK_HELD, "프로젝트가 실행 중이다")
            self._prune_locks(db, project)
            if set(selected).intersection(self._blocked_targets(db, project)):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "환경 상태를 사람이 확인해야 한다"
                )
            now = time.time()
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
            db.execute(
                "INSERT OR REPLACE INTO run_targets VALUES (?, ?)", (run_id, _json(selected))
            )
        return token

    def assert_idle(self, project: str) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM runs WHERE project=? AND status='RUNNING'", (project,)
            ).fetchone():
                raise DdakToolError(ErrorCode.LOCK_HELD, "다른 실행/복구가 진행 중이다")
            self._prune_locks(db, project)
            if db.execute("SELECT 1 FROM locks WHERE project=?", (project,)).fetchone():
                raise DdakToolError(ErrorCode.LOCK_HELD, "다른 실행/복구가 진행 중이다")
            if self._blocked_targets(db, project):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "환경 상태를 사람이 확인해야 한다"
                )

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
            return self._environments(db, project)

    @staticmethod
    def _environments(db: sqlite3.Connection, project: str) -> dict[str, dict[str, Any]]:
        rows = db.execute("SELECT * FROM env_release WHERE project=?", (project,)).fetchall()
        return {
            r["target"]: {
                "status": r["status"],
                "current": json.loads(r["current"]) if r["current"] else None,
                "previous": json.loads(r["previous"]) if r["previous"] else None,
            }
            for r in rows
        }

    def project_state(self, project: str) -> dict[str, Any]:
        """운영 조회: 인프라 원상태와 수동 차단 면제를 구분하며 잠금 토큰은 노출하지 않는다."""
        with self.connection() as db:
            db.execute("BEGIN")
            lock = db.execute(
                "SELECT run_id, heartbeat, expires FROM locks WHERE project=?", (project,)
            ).fetchone()
            runs = db.execute(
                "SELECT run_id, status FROM runs WHERE project=? "
                "AND status IN ('RUNNING', 'NEEDS_HUMAN') ORDER BY created, run_id",
                (project,),
            ).fetchall()
            return {
                "project": project,
                "lock": {**dict(lock), "expired": lock["expires"] <= time.time()} if lock else None,
                "active_runs": [row["run_id"] for row in runs if row["status"] == "RUNNING"],
                "needs_human_runs": [
                    row["run_id"] for row in runs if row["status"] == "NEEDS_HUMAN"
                ],
                "blocked_targets": self._blocked_targets(db, project),
                "environments": self._environments(db, project),
            }

    def unlock_project(self, project: str, actor: str, reason: str) -> dict[str, Any]:
        """controller lease와 service task 검사가 선행돼야 한다. 복구가 아닌 재시도 허용이다."""
        if not all(isinstance(value, str) and value.strip() for value in (project, actor, reason)):
            raise ValueError("project, actor, reason은 비어 있을 수 없다")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM runs WHERE project=? AND status='RUNNING'", (project,)
            ).fetchone():
                raise DdakToolError(ErrorCode.LOCK_HELD, "실행 중인 프로젝트는 수동 해제할 수 없다")
            lock = db.execute("SELECT run_id FROM locks WHERE project=?", (project,)).fetchone()
            if (
                lock is not None
                and not db.execute("SELECT 1 FROM runs WHERE run_id=?", (lock[0],)).fetchone()
            ):
                self._needs_human(db, project, _targets(None))
            # 오래된 장부의 NEEDS_HUMAN 잠금도 환경 차단으로 먼저 옮긴다.
            self._prune_locks(db, project)
            cleared = [
                row[0]
                for row in db.execute(
                    "SELECT target FROM env_release WHERE project=? AND status='NEEDS_HUMAN' "
                    "AND NOT EXISTS (SELECT 1 FROM unlock_ack "
                    "WHERE unlock_ack.project=env_release.project "
                    "AND unlock_ack.target=env_release.target) ORDER BY target",
                    (project,),
                )
            ]
            for target in cleared:
                db.execute("INSERT INTO unlock_ack VALUES (?, ?)", (project, target))
            db.execute("DELETE FROM locks WHERE project=?", (project,))
            detail = {
                "released_lock": lock[0] if lock else None,
                "cleared_targets": cleared,
                "unlocked": lock is not None or bool(cleared),
                "recovery_verified": False,
            }
            now = time.time()
            safe = redact_obj({"actor": actor.strip(), "reason": reason.strip()})
            cursor = db.execute(
                "INSERT INTO unlock_audit(project, actor, reason, created, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (project, safe["actor"], safe["reason"], now, _json(detail)),
            )
            return {"id": cursor.lastrowid, "project": project, **safe, "created": now, **detail}

    def unlock_history(self, project: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM unlock_audit WHERE project=? ORDER BY id DESC LIMIT ?",
                (project, max(1, min(limit, 100))),
            ).fetchall()
        history = []
        for row in rows:
            entry = dict(row)
            entry.update(json.loads(entry.pop("detail")))
            history.append(entry)
        return history

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
                db.execute("DELETE FROM unlock_ack WHERE project=? AND target=?", (run[0], target))
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
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT project, status FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] == "SUPERSEDED":
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "SUPERSEDED: 새 소스로 대체된 실행이다"
                )
            if status == "NEEDS_HUMAN":
                self._needs_human(db, row["project"], self._run_targets(db, run_id))
            db.execute(
                "UPDATE runs SET status=?, finished=? WHERE run_id=?", (status, time.time(), run_id)
            )

    def recover_interrupted(self) -> list[str]:
        """컨트롤러 독점 lease 획득 뒤 기동할 때만 호출한다. 인프라 복구 성공을 뜻하지 않는다."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT run_id, project FROM runs WHERE status='RUNNING'").fetchall()
            for row in rows:
                self._needs_human(db, row["project"], self._run_targets(db, row["run_id"]))
            for lock in db.execute(
                "SELECT locks.project, locks.run_id, runs.status FROM locks "
                "LEFT JOIN runs ON locks.run_id=runs.run_id"
            ).fetchall():
                if lock["status"] is None:
                    self._needs_human(db, lock["project"], _targets(None))
                elif (
                    lock["status"] == "NEEDS_HUMAN"
                    and not db.execute(
                        "SELECT 1 FROM releases WHERE run_id=?", (lock["run_id"],)
                    ).fetchone()
                ):
                    self._needs_human(db, lock["project"], self._run_targets(db, lock["run_id"]))
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
            db.execute("DELETE FROM locks")
        return [r[0] for r in rows]
