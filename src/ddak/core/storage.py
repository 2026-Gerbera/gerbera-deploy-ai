"""저장소 통합 계약. 소스 탐지는 탐지 작업의 구현으로 병합한다."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

STORAGE_ENV_KEY = "IMG_DIR"
OUTPUT_KEY = "upload_bucket"


@dataclass(frozen=True)
class StorageEvidence:
    file: str
    line: int
    kind: Literal["hardcoded_dir", "env_read", "file_write"]


def bucket_name(platform: str, account_id: str) -> str:
    return f"ddak-{platform}-uploads-{account_id}"


def storage_intent(needs: bool, present: bool) -> Literal["create", "remove"] | None:
    if needs == present:
        return None
    return "create" if needs else "remove"


def scan_storage(files: Mapping[str, str]) -> list[StorageEvidence]:
    raise NotImplementedError("저장소 탐지 구현을 병합해야 한다")
