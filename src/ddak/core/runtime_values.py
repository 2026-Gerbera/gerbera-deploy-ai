"""설정값 정책. 공개 파생값과 키 이름만 승인 데이터로 반환한다."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ddak.core.contracts.errors import DdakToolError, ErrorCode

_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_GENERATED = re.compile(r"(?:[A-Z0-9_]+_)?SECRET_KEY\Z")
PIPELINE_KEYS = frozenset({"RELEASE_ID", "SOURCE_SHA"})


def generated_secret(name: str) -> bool:
    return bool(_GENERATED.fullmatch(name))


def key_names(path: Path) -> set[str]:
    """실행 제품이 권한 제한 파일의 키 이름만 읽는다. 내용은 반환/로그하지 않는다."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return set()
    with os.fdopen(fd) as stream:
        meta = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != os.getuid()
            or stat.S_IMODE(meta.st_mode) != 0o600
            or meta.st_size > 65536
        ):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "환경 값 파일의 소유권/권한/크기 오류")
        result = set()
        for line in stream:
            if not line.strip() or line.startswith("#"):
                continue
            name, sep, value = line.rstrip("\n").partition("=")
            if not sep or not _KEY.fullmatch(name):
                raise DdakToolError(ErrorCode.CONFIG_INVALID, "환경 값 파일 형식 오류")
            if value:
                result.add(name)
        return result


def derived_public(inventory: Mapping[str, Any], keys: Sequence[str]) -> dict[str, str]:
    known = {}
    url = inventory.get("public_url")
    if url:
        known["APP_BASE_URL"] = url
        known["SESSION_COOKIE_SECURE"] = "true" if urlsplit(url).scheme == "https" else "false"
        # 터널->nginx->WAS proxy hop 값은 등록된 인벤토리에서만 읽는다. 추측하지 않는다.
    db = inventory.get("tiers", {}).get("db", {}).get("host", {})
    if isinstance(db, Mapping) and db.get("host"):
        known["DB_HOST"] = db["host"]
    return {key: known[key] for key in keys if key in known}


def local_missing(inventory: Mapping[str, Any], keys: Sequence[str]) -> list[str]:
    was = inventory.get("tiers", {}).get("was", {})
    provided = set(was.get("public_env", {})) | set(derived_public(inventory, keys)) | PIPELINE_KEYS
    if was.get("env_file"):
        provided |= key_names(Path(was["env_file"]))
    return sorted(k for k in keys if k not in provided and not generated_secret(k))
