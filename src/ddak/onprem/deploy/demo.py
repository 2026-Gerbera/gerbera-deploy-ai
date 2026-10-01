"""전용 데모 프로젝트를 봉인된 v1 릴리스로 복구하는 운영 스크립트 경계."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.store import Store, release_view
from ddak.onprem.deploy.config import env_key_names, remove_demo_secret
from ddak.onprem.deploy.containers import Runner, fail, image_ref, subprocess_runner
from ddak.onprem.deploy.provider import OnPremProvider, _Inventory
from ddak.onprem.deploy.replicas import names

DEMO_PROJECTS = frozenset({"flaskr"})
_RELEASE_KEYS = (
    "release_id",
    "source_mode",
    "source",
    "files",
    "source_files",
    "images",
    "artifacts",
)


def _guard(db: sqlite3.Connection) -> None:
    if (
        db.execute("SELECT 1 FROM locks LIMIT 1").fetchone()
        or db.execute(
            "SELECT 1 FROM runs WHERE status IN ('RUNNING','NEEDS_HUMAN') LIMIT 1"
        ).fetchone()
        or db.execute(
            "SELECT 1 FROM env_release WHERE status IN ('RUNNING','NEEDS_HUMAN') LIMIT 1"
        ).fetchone()
    ):
        raise fail("RUNNING/NEEDS_HUMAN/잠금은 수동 확인 필요", ErrorCode.PRECONDITION_FAILED)


def _audit(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def reset_demo(
    state: Path,
    project: str,
    inventory: dict[str, Any],
    release_id: str,
    *,
    runner: Runner = subprocess_runner,
) -> dict[str, Any]:
    if os.environ.get("ALLOW_DEMO_RESET") != "1" or project not in DEMO_PROJECTS:
        raise fail(
            "명시적 데모 reset 허용과 프로젝트 allowlist 필요", ErrorCode.PRECONDITION_FAILED
        )
    state = state.absolute()
    db_path = state / "ddak.sqlite"
    if state.is_symlink() or db_path.is_symlink() or not db_path.is_file():
        raise fail("실행 상태 경로 오류", ErrorCode.PRECONDITION_FAILED)
    inventory_model = _Inventory.model_validate(inventory)
    fd = os.open(state / "controller.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lease:
        meta = os.fstat(lease.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_nlink != 1:
            raise fail("controller lock 파일 오류", ErrorCode.PRECONDITION_FAILED)
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise fail("활성 컨트롤러가 있어 reset 거부", ErrorCode.PRECONDITION_FAILED) from None
        with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as db:
            _guard(db)
            row = db.execute(
                "SELECT runs.project,runs.status,releases.manifest FROM releases "
                "JOIN runs USING(run_id) WHERE run_id=?",
                (release_id,),
            ).fetchone()
            if row is None or row[0] != project or row[1] != "SUCCEEDED":
                raise fail(
                    "같은 프로젝트의 성공한 기준 release 필요", ErrorCode.PRECONDITION_FAILED
                )
            manifest = json.loads(row[2])
            if (
                manifest.get("source_mode") != "real"
                or manifest.get("release_id") != release_id
                or manifest.get("result", {}).get("tracks", {}).get("local") != "DONE"
            ):
                raise fail("REAL local 성공 release 필요", ErrorCode.PRECONDITION_FAILED)
            if not isinstance(manifest.get("images"), dict) or not manifest["images"]:
                raise fail("기준 release 이미지 없음", ErrorCode.CONFIG_INVALID)
            for tier, ref in manifest["images"].items():
                image_ref(ref)
                if tier not in inventory_model.tiers:
                    raise fail("기준 release tier 인벤토리 없음", ErrorCode.CONFIG_INVALID)
            baseline = release_view({key: manifest[key] for key in _RELEASE_KEYS})
        provider = OnPremProvider(runner)
        ctx = RunContext(
            "demo-reset",
            project=project,
            adapter_mode=AdapterMode.REAL,
            images=baseline["images"],
            previous_release={"local": baseline},
            platform={"onprem": inventory},
            deadline=time.monotonic() + 300,
        )
        # 모든 env·소유 라벨을 먼저 확인해 잘못된 대상의 키를 지우지 않는다.
        for tier in baseline["images"]:
            with provider._session(tier, ctx) as (host, config):
                if config.env_file:
                    env_key_names(Path(config.env_file))
                for _, name in names(config):
                    for candidate in (name, name + "-ddak-next"):
                        existing = host.container(candidate)
                        if existing:
                            host.owned(existing, project, tier)
        store = Store(db_path)
        before = store.environments(project)
        audit_dir = state / "ops"
        if audit_dir.is_symlink():
            raise fail("ops 감사 경로 오류", ErrorCode.PRECONDITION_FAILED)
        audit_dir.mkdir(mode=0o700, exist_ok=True)
        audit_path = audit_dir / ("reset-" + uuid.uuid4().hex + ".json")
        report: dict[str, Any] = {
            "status": "RUNNING",
            "project": project,
            "release_id": release_id,
            "before": before,
            "replicas": {},
            "source": "real-demo-reset",
            "warning": "DB 역마이그레이션 생략. 0002 적용 상태면 다음 prepare_db는 applied=[]",
        }
        _audit(audit_path, report)
        try:
            removed = False
            for tier in baseline["images"]:
                config = inventory_model.tiers[tier]
                if config.env_file:
                    removed = remove_demo_secret(Path(config.env_file)) or removed
            for tier in baseline["images"]:
                result = provider.rollback(tier, ctx)
                report["replicas"][tier] = {"changed": result.changed, "detail": result.detail}
            with store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                _guard(db)
                old = db.execute(
                    "SELECT current,previous FROM env_release WHERE project=? AND target='local'",
                    (project,),
                ).fetchone()
                current = json.dumps(baseline, ensure_ascii=False)
                prior = old[0] if old else None
                if old and json.loads(old[0] or "null") == baseline:
                    prior = old[1]
                db.execute(
                    "INSERT OR REPLACE INTO env_release VALUES (?, 'local', 'SUCCEEDED', ?, ?)",
                    (project, current, prior),
                )
                db.execute(
                    "UPDATE env_release SET status='ROLLED_BACK' "
                    "WHERE project=? AND target='cloud'",
                    (project,),
                )
            report.update(
                status="SUCCEEDED", after=store.environments(project), secret_key_removed=removed
            )
            _audit(audit_path, report)
            return report
        except Exception as error:
            # 부분 복구 뒤 다음 배포가 장부만 믿고 진행하지 못하게 남긴다.
            with store.connection() as db:
                db.execute(
                    "INSERT INTO env_release(project,target,status) "
                    "VALUES (?, 'local', 'NEEDS_HUMAN') "
                    "ON CONFLICT(project,target) DO UPDATE SET status='NEEDS_HUMAN'",
                    (project,),
                )
            report.update(
                status="NEEDS_HUMAN",
                error=error.code.value
                if isinstance(error, DdakToolError)
                else type(error).__name__,
            )
            _audit(audit_path, report)
            raise fail(
                "demo reset 실패: 감사 기록과 대상 상태 수동 확인 필요",
                ErrorCode.PRECONDITION_FAILED,
            ) from None
