"""권한 제한 env 파일 검증과 공개 설정·시크릿 주입. 값은 반환하지 않는다.

--env-file 값은 대상 Docker inspect로 보인다. Docker 그룹 권한은 root와 동등하다.
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ddak.cd.interface import ProviderResult
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import ErrorCode
from ddak.onprem.deploy.containers import fail

if TYPE_CHECKING:
    from ddak.onprem.deploy.provider import OnPremProvider

_IDENTIFIER = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*\Z")


def _private_env(
    path: Path,
    keys: Sequence[str] | None = None,
    public_env: Mapping[str, str] | None = None,
) -> bool:
    """0600 단일 소유 일반 파일만 허용한다. keys=None이면 내용은 읽지 않는다."""
    changed = False
    try:
        if keys is not None:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            if keys is None:
                raise
            try:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                fd = os.open(path, flags)
            else:
                changed = True
        with os.fdopen(fd, "r+", encoding="utf-8") as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_uid != os.getuid():
                raise fail("env 파일 소유권/형식 오류", ErrorCode.CONFIG_INVALID)
            # 기다리는 파일 lock 때문에 실행 deadline을 초과하지 않는다.
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if stat.S_IMODE(meta.st_mode) != 0o600:
                if keys is None:
                    raise fail("env 파일 권한은 0600이어야 한다", ErrorCode.CONFIG_INVALID)
                os.fchmod(stream.fileno(), 0o600)
                changed = True
            if keys is None:
                return False
            if meta.st_size > 65536:
                raise fail("env 파일 크기 초과", ErrorCode.CONFIG_INVALID)
            original = stream.read()
            values: dict[str, str] = {}
            for line in original.splitlines():
                if not line or line.startswith("#"):
                    continue
                key, sep, value = line.partition("=")
                if not sep or not _IDENTIFIER.fullmatch(key) or key in values or "\x00" in value:
                    raise fail("env 파일 형식 오류", ErrorCode.CONFIG_INVALID)
                values[key] = value
            old_values = dict(values)
            values.update(public_env or {})
            if any(key != "SECRET_KEY" and not values.get(key) for key in keys):
                raise fail("요청한 환경 키가 host env 파일에 없다", ErrorCode.CONFIG_INVALID)
            if "SECRET_KEY" in keys:
                if "SECRET_KEY" in values:
                    if not re.fullmatch(r"[0-9a-f]{64}", values["SECRET_KEY"]):
                        raise fail(
                            "기존 SECRET_KEY는 64자리 hex여야 한다", ErrorCode.CONFIG_INVALID
                        )
                else:
                    values["SECRET_KEY"] = secrets.token_hex(32)
            if values != old_values:
                lines = original.splitlines()
                updated = [
                    line.partition("=")[0] + "=" + values[line.partition("=")[0]]
                    if line and not line.startswith("#")
                    else line
                    for line in lines
                ]
                updated.extend(
                    f"{key}={value}" for key, value in values.items() if key not in old_values
                )
                stream.seek(0)
                stream.write("\n".join(updated) + "\n")
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
                changed = True
        return changed
    except (OSError, UnicodeError):
        raise fail("host env 파일 접근 실패", ErrorCode.CONFIG_INVALID) from None


def inject_config(provider: OnPremProvider, keys: Sequence[str], ctx: RunContext) -> ProviderResult:
    host, config = provider._runtime("was", ctx)
    wanted = sorted(set(keys) | set(config.public_env))
    if any(not _IDENTIFIER.fullmatch(key) for key in wanted) or not config.env_file:
        raise fail("env key 또는 env_file 설정 오류", ErrorCode.CONFIG_INVALID)
    changed = _private_env(Path(config.env_file), wanted, config.public_env)
    host.check_deadline()
    return ProviderResult(
        provider=provider.name,
        function="inject_config",
        changed=changed,
        config_ref=config.env_file,
        keys=wanted,
    )


def env_key_names(path: Path) -> list[str]:
    """spec에 사용할 키 이름만 반환한다. 비밀값은 해시에 포함하지 않는다."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, encoding="utf-8") as stream:
            meta = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(meta.st_mode)
                or meta.st_uid != os.getuid()
                or meta.st_nlink != 1
                or stat.S_IMODE(meta.st_mode) != 0o600
                or meta.st_size > 65536
            ):
                raise fail("env 파일 소유권/형식/권한 오류", ErrorCode.CONFIG_INVALID)
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            keys = []
            for line in stream:
                if not line.strip() or line.startswith("#"):
                    continue
                key, sep, _ = line.partition("=")
                if not sep or not _IDENTIFIER.fullmatch(key) or key in keys or "\0" in line:
                    raise fail("env 파일 형식 오류", ErrorCode.CONFIG_INVALID)
                keys.append(key)
            return sorted(keys)
    except (OSError, UnicodeError):
        raise fail("host env 파일 접근 실패", ErrorCode.CONFIG_INVALID) from None


def remove_demo_secret(path: Path) -> bool:
    """보호된 데모 reset 전용: 다른 줄을 보존하고 SECRET_KEY만 제거한다."""
    env_key_names(path)  # 쓰기 전에 동일한 형식·권한 검사
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r+", encoding="utf-8") as stream:
            meta = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(meta.st_mode)
                or meta.st_uid != os.getuid()
                or meta.st_nlink != 1
                or stat.S_IMODE(meta.st_mode) != 0o600
            ):
                raise fail("env 파일 소유권/권한 오류", ErrorCode.CONFIG_INVALID)
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            original = stream.read(65537)
            if len(original) > 65536:
                raise fail("env 파일 크기 초과", ErrorCode.CONFIG_INVALID)
            updated = "".join(
                line
                for line in original.splitlines(keepends=True)
                if not line.startswith("SECRET_KEY=")
            )
            if original == updated:
                return False
            stream.seek(0)
            stream.write(updated)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
            return True
    except (OSError, UnicodeError):
        raise fail("demo env 초기화 실패", ErrorCode.CONFIG_INVALID) from None
