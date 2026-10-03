"""제품 전용 값 저장소. path는 폴더이며 공개 API는 값을 로그하지 않는다."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Iterator, Mapping
from pathlib import Path

from ddak.core.contracts.errors import DdakToolError, ErrorCode

_MAX_VALUE = 65536
_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")


def private_error() -> DdakToolError:
    return DdakToolError(ErrorCode.CONFIG_INVALID, "제품 전용 저장 경로 또는 값 형식 오류")


def validated_path(path: str | Path) -> Path:
    """정규화로 '..'나 링크를 숨기지 않는다. 오류에 입력 경로를 넣지 않는다."""
    try:
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError
        if any(part in {".", ".."} for part in raw.split("/")):
            raise ValueError
        result = Path(raw).absolute()
        if result == Path("/"):
            raise ValueError
        # 기존 구성요소는 내용 없이 검사한다. 없는 꼬리는 허용한다.
        parent = Path("/")
        for part in result.parts[1:]:
            parent /= part
            try:
                mode = parent.lstat().st_mode
            except FileNotFoundError:
                break
            if not stat.S_ISDIR(mode):
                raise ValueError
        return result
    except (OSError, TypeError, ValueError):
        raise private_error() from None


@contextlib.contextmanager
def private_directory(path: str | Path, *, create: bool = False) -> Iterator[int]:
    """dir_fd + O_NOFOLLOW로 각 구성요소를 열어 경로 교체를 따라가지 않는다."""
    target = validated_path(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in target.parts[1:]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=fd)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        meta = os.fstat(fd)
        if meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) != 0o700:
            raise private_error()
        yield fd
    except FileNotFoundError:
        raise
    except OSError:
        raise private_error() from None
    finally:
        os.close(fd)


def _file_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or any(c in name for c in "/\\\x00")
        or len(name.encode("utf-8")) > 255
    ):
        raise private_error()


def read_private(fd: int, name: str, *, mode: int = 0o600, limit: int = _MAX_VALUE) -> bytes:
    _file_name(name)
    stream = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        meta = os.fstat(stream)
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != os.getuid()
            or stat.S_IMODE(meta.st_mode) != mode
            or meta.st_nlink != 1
            or meta.st_size > limit
        ):
            raise private_error()
        with os.fdopen(stream, "rb", closefd=False) as source:
            value = source.read(limit + 1)
        if len(value) > limit:
            raise private_error()
        return value
    finally:
        os.close(stream)


def write_private(fd: int, name: str, data: bytes, *, mode: int = 0o600) -> None:
    # 기존 링크·FIFO·공유 하드링크에는 덮어쓰지 않는다.
    _file_name(name)
    try:
        old = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        old = None
    if old is not None and (
        not stat.S_ISREG(old.st_mode)
        or old.st_uid != os.getuid()
        or old.st_nlink != 1
        or stat.S_IMODE(old.st_mode) != mode
    ):
        raise private_error()
    temporary = ".pending-" + secrets.token_hex(16)
    stream = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd
    )
    try:
        os.fchmod(stream, mode)
        with os.fdopen(stream, "wb", closefd=False) as destination:
            destination.write(data)
            destination.flush()
        os.fsync(stream)
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(stream)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=fd)


class SecretVault:
    """프로젝트·키를 해시 파일명으로 분리한다. get은 제품 실행 경로에서만 쓴다."""

    def __init__(self, path: str | Path) -> None:
        self.path = validated_path(path)

    def __repr__(self) -> str:
        return "<SecretVault>"

    @staticmethod
    def _name(project: str, key: str) -> str:
        if any(not isinstance(v, str) or not _IDENTIFIER.fullmatch(v) for v in (project, key)):
            raise private_error()
        return hashlib.sha256((project + "\x00" + key).encode()).hexdigest() + ".value"

    def put(self, project: str, key: str, value: str) -> None:
        name = self._name(project, key)
        if not isinstance(value, str) or not value or "\x00" in value:
            raise private_error()
        try:
            data = value.encode("utf-8")
            if len(data) > _MAX_VALUE:
                raise private_error()
            with private_directory(self.path, create=True) as fd:
                fcntl.flock(fd, fcntl.LOCK_EX)
                write_private(fd, name, data)
        except (OSError, UnicodeError):
            raise private_error() from None

    def get(self, project: str, key: str) -> str | None:
        name = self._name(project, key)
        try:
            with private_directory(self.path) as fd:
                fcntl.flock(fd, fcntl.LOCK_SH)
                value = read_private(fd, name).decode("utf-8")
                if not value or "\x00" in value:
                    raise private_error()
                return value
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError):
            raise private_error() from None

    def delete(self, project: str, key: str) -> bool:
        name = self._name(project, key)
        try:
            with private_directory(self.path) as fd:
                fcntl.flock(fd, fcntl.LOCK_EX)
                read_private(fd, name)  # 링크·권한이 잘못되면 삭제도 거부한다.
                os.unlink(name, dir_fd=fd)
                os.fsync(fd)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            raise private_error() from None

    def configured(self, project: str, key: str) -> bool:
        return self.get(project, key) is not None

    def export_env(self, project: str, path: str | Path, keys: Mapping[str, str]) -> None:
        """환경 이름 -> vault 키. 누락·줄바꿈 값은 전체 기록 전에 차단한다."""
        try:
            raw = os.fspath(path)
            if not isinstance(raw, str) or "\x00" in raw:
                raise private_error()
            if any(part in {".", ".."} for part in raw.split("/")):
                raise private_error()
            target = Path(raw).absolute()
            parent = validated_path(target.parent)
            if not target.name or not isinstance(keys, Mapping) or not keys:
                raise private_error()
            lines = []
            for name, key in sorted(keys.items()):
                if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name):
                    raise private_error()
                value = self.get(project, key)
                if value is None or "\n" in value or "\r" in value:
                    raise private_error()
                lines.append(name + "=" + value + "\n")
            data = "".join(lines).encode("utf-8")
            if len(data) > _MAX_VALUE:
                raise private_error()
            with private_directory(parent, create=True) as fd:
                write_private(fd, target.name, data)
        except (OSError, TypeError, ValueError):
            raise private_error() from None
