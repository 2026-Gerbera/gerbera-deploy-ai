"""관리자 인벤토리 등록. 키·env 내용은 열지 않고 제품 JSON만 원자적으로 저장한다."""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ddak.core.contracts.errors import ErrorCode
from ddak.onprem.deploy.containers import fail
from ddak.onprem.deploy.provider import _Inventory

_LIMIT = 65536


def _validate(data: Any) -> _Inventory:
    try:
        return _Inventory.model_validate(data)
    except (ValidationError, ValueError, TypeError):
        # pydantic 오류에는 입력값이 들어갈 수 있다. 원본 오류도 노출하지 않는다.
        raise fail("onprem 인벤토리 형식 오류", ErrorCode.CONFIG_INVALID) from None


def _decode(raw: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        if len(raw.encode("utf-8")) > _LIMIT:
            raise ValueError("size")
        data = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(data, dict):
            raise ValueError("object")
        return data
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise fail("onprem JSON 형식 오류", ErrorCode.CONFIG_INVALID) from None


def _defaults(data: dict[str, Any]) -> dict[str, Any]:
    # 값은 검증·저장 경계에서만 다루고 repr/log/오류에 넣지 않는다.
    tiers = data.get("tiers")
    if isinstance(tiers, dict):
        for tier in tiers.values():
            if isinstance(tier, dict) and tier.get("kind") == "nginx":
                if tier.get("ready") is None:
                    tier["ready"] = {"port": 8080, "path": "/nginx-health"}
                if isinstance(tier.get("ready"), dict):
                    tier["ready"].setdefault("port", 8080)
                    tier["ready"].setdefault("path", "/nginx-health")
    return data


def _directory(path: Path, *, create: bool = True) -> int:
    """각 경로 요소를 NOFOLLOW로 열고 최종 디렉토리 fd를 유지한다."""
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise fail("인벤토리 저장 경로 오류", ErrorCode.CONFIG_INVALID)
    fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=fd)
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        meta = os.fstat(fd)
        if meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) & 0o022:
            raise fail("인벤토리 디렉토리 소유권/권한 오류", ErrorCode.CONFIG_INVALID)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write(path: Path, inventory: _Inventory) -> Path:
    payload = inventory.model_dump_json(indent=2).encode("utf-8") + b"\n"
    if len(payload) > _LIMIT:
        raise fail("인벤토리 크기 초과", ErrorCode.CONFIG_INVALID)
    directory = -1
    temporary = ".inventory-" + secrets.token_hex(16)
    try:
        directory = _directory(path.parent)
        try:
            old = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(old.st_mode) or old.st_uid != os.getuid() or old.st_nlink != 1:
                raise fail("인벤토리 대상 파일 오류", ErrorCode.CONFIG_INVALID)
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
        return path.absolute()
    except OSError:
        raise fail("인벤토리 저장 실패", ErrorCode.CONFIG_INVALID) from None
    finally:
        if directory >= 0:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
            os.close(directory)


def register_onprem_inventory(inventory_json: str, destination: Path) -> Path:
    """JSON 전체 검증 후 0600 atomic replace. SSH 키·env 파일을 읽거나 만들지 않는다."""
    return _write(destination, _validate(_defaults(_decode(inventory_json))))


def write_inventory(root: Path, project: str, data: dict[str, Any]) -> Path:
    """main 설정 저장 hook. env 내용 생성은 호출자의 책임이다."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", project):
        raise fail("프로젝트 식별자 오류", ErrorCode.CONFIG_INVALID)
    try:
        copied = _decode(json.dumps(data, allow_nan=False))
    except (ValueError, TypeError, RecursionError):
        raise fail("onprem JSON 형식 오류", ErrorCode.CONFIG_INVALID) from None
    root = root.absolute()
    tiers = copied.get("tiers")
    if isinstance(tiers, dict) and isinstance(tiers.get("was"), dict):
        was = tiers["was"]
        was.setdefault("env_file", str(root / "private" / project / "runtime.env"))
        was.setdefault("migration_env_file", str(root / "private" / project / "migration.env"))
    return _write(root / "settings" / project / "inventory.json", _validate(_defaults(copied)))


def read_inventory(path: Path) -> _Inventory:
    """제품 소유 0600 인벤토리만 읽고 재검증해 공개 InventoryConfig를 반환한다."""
    directory = -1
    try:
        # read는 디렉토리를 생성하지 않는다.
        directory = _directory(path.parent, create=False)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            meta = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(meta.st_mode)
                or meta.st_uid != os.getuid()
                or meta.st_nlink != 1
                or stat.S_IMODE(meta.st_mode) != 0o600
                or meta.st_size > _LIMIT
            ):
                raise fail("인벤토리 파일 소유권/형식/권한 오류", ErrorCode.CONFIG_INVALID)
            return _validate(_decode(stream.read(_LIMIT + 1)))
    except (OSError, UnicodeError):
        raise fail("인벤토리 읽기 실패", ErrorCode.CONFIG_INVALID) from None
    finally:
        if directory >= 0:
            os.close(directory)
