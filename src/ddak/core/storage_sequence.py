"""프로젝트별 업로드 버킷 번호 예약. 실패하거나 삭제한 번호도 다시 쓰지 않는다."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.storage import bucket_name, bucket_prefix, valid_bucket

MAX_BUCKET_ATTEMPTS = 10


@contextmanager
def _database(root: Path) -> Iterator[sqlite3.Connection]:
    directory = root / "storage"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "bucket-sequence.sqlite3"
    if path.is_symlink() or directory.is_symlink():
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "버킷 순번 경로는 symlink일 수 없다")
    db = sqlite3.connect(path, timeout=30)
    path.chmod(0o600)
    try:
        db.executescript(
            "CREATE TABLE IF NOT EXISTS storage_sequences "
            "(project TEXT PRIMARY KEY, last_number INTEGER NOT NULL);"
            "CREATE TABLE IF NOT EXISTS storage_reservations "
            "(project TEXT, run_id TEXT, bucket TEXT NOT NULL, PRIMARY KEY(project,run_id));"
        )
        yield db
    finally:
        db.close()


def remember_bucket(root: Path, project: str, platform: str, bucket: str) -> None:
    """삭제 전에 현재 출력의 순번을 보존한다. 삭제 성공 여부와 무관하게 유지한다."""
    if not valid_bucket(platform, bucket):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장된 업로드 버킷 이름이 잘못됐다")
    number = int(bucket.removeprefix(bucket_prefix(platform)))
    with _database(root) as db, db:
        db.execute(
            "INSERT INTO storage_sequences VALUES (?, ?) ON CONFLICT(project) "
            "DO UPDATE SET last_number=max(last_number, excluded.last_number)",
            (project, number),
        )


def reserve_bucket(
    root: Path,
    project: str,
    platform: str,
    run_id: str,
    probe: Callable[[str], int],
) -> str:
    """404만 예약한다. 이미 쓰는 이름(200/403)은 건너뛰며 조회 오류는 중단한다."""
    bucket_prefix(platform)
    with _database(root) as db:
        for _ in range(MAX_BUCKET_ATTEMPTS):
            with db:
                db.execute("BEGIN IMMEDIATE")
                saved = db.execute(
                    "SELECT bucket FROM storage_reservations WHERE project=? AND run_id=?",
                    (project, run_id),
                ).fetchone()
                if saved:
                    if not valid_bucket(platform, saved[0]):
                        raise DdakToolError(
                            ErrorCode.PRECONDITION_FAILED, "예약한 버킷의 플랫폼이 바뀌었다"
                        )
                    return saved[0]
                row = db.execute(
                    "SELECT last_number FROM storage_sequences WHERE project=?", (project,)
                ).fetchone()
                number = (row[0] if row else 0) + 1
                try:
                    bucket = bucket_name(platform, number)
                except ValueError:
                    raise DdakToolError(
                        ErrorCode.PRECONDITION_FAILED, "업로드 버킷 순번 한도를 소진했다"
                    ) from None
                db.execute(
                    "INSERT INTO storage_sequences VALUES (?, ?) ON CONFLICT(project) "
                    "DO UPDATE SET last_number=excluded.last_number",
                    (project, number),
                )
            # 조회 전에 번호를 저장하므로 오류·프로세스 중단 때도 재사용하지 않는다.
            status = probe(bucket)
            if status in (200, 403):
                continue
            if status != 404:
                raise DdakToolError(
                    ErrorCode.ADAPTER_FAILED, "업로드 버킷 이름 조회 실패; 다시 준비하세요"
                )
            with db:
                db.execute(
                    "INSERT OR IGNORE INTO storage_reservations VALUES (?, ?, ?)",
                    (project, run_id, bucket),
                )
                row = db.execute(
                    "SELECT bucket FROM storage_reservations WHERE project=? AND run_id=?",
                    (project, run_id),
                ).fetchone()
                return row[0]
    raise DdakToolError(
        ErrorCode.PRECONDITION_FAILED, "사용 가능한 업로드 버킷 이름을 찾지 못했다; 다시 준비하세요"
    )
