"""일회성 pinned SSH 설정. 개인 키 내용과 사용자 SSH 설정은 읽지 않는다."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ddak.core.contracts.errors import ErrorCode
from ddak.onprem.deploy.containers import Runner, fail


class JumpHost(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    host: str
    user: str
    port: int = Field(default=22, ge=1, le=65535)
    host_key_fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")

    @field_validator("host")
    @classmethod
    def hostname(cls, value: str) -> str:
        if len(value) > 253 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", value):
            raise ValueError("SSH host 형식 오류")
        return value

    @field_validator("user")
    @classmethod
    def username(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", value):
            raise ValueError("SSH user 형식 오류")
        return value


class SSHConfig(JumpHost):
    key_path: str
    jump_host: JumpHost | None = None

    @field_validator("key_path")
    @classmethod
    def key_location(cls, value: str) -> str:
        if not Path(value).is_absolute() or any(c in value for c in "\n\r\0%$"):
            raise ValueError("SSH key_path 절대 경로 필요")
        return value

    def check_key(self) -> None:
        try:
            meta = Path(self.key_path).lstat()
            if (
                not stat.S_ISREG(meta.st_mode)
                or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) & 0o077
            ):
                raise fail("SSH key_path 소유권/권한 오류", ErrorCode.CONFIG_INVALID)
        except OSError:
            raise fail("SSH key_path stat 실패", ErrorCode.CONFIG_INVALID) from None


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _block(alias: str, host: JumpHost, key_path: str, known: Path) -> str:
    return f"""Host {alias}
    HostName {host.host}
    Port {host.port}
    User {host.user}
    HostKeyAlias {alias}
    IdentityFile {_quote(key_path)}
    IdentitiesOnly yes
    StrictHostKeyChecking yes
    UserKnownHostsFile {_quote(str(known))}
    GlobalKnownHostsFile /dev/null
    BatchMode yes
    ConnectTimeout 10
    ServerAliveInterval 10
"""


def _pinned(raw: str, alias: str, expected: str) -> str:
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[0].startswith("#"):
            continue
        try:
            blob = base64.b64decode(parts[2], validate=True)
        except (ValueError, binascii.Error):
            continue
        actual = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        if actual == expected:
            return f"{alias} {parts[1]} {parts[2]}\n"
    raise fail("호스트 키 불일치", ErrorCode.CONFIG_INVALID)


def _private_write(path: Path, data: str, mode: int = 0o600) -> None:
    # 소유한 임시 디렉토리 안에서만 생성한다.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as stream:
        stream.write(data)


@contextmanager
def session(config: SSHConfig, runner: Runner, deadline: float) -> Iterator[Runner]:
    config.check_key()
    ssh = shutil.which("ssh")
    if not ssh:
        raise fail("SSH CLI 없음", ErrorCode.CONFIG_INVALID)
    directory = Path(tempfile.mkdtemp(prefix="ddak-ssh-"))
    os.chmod(directory, 0o700)
    cfg, known = directory / "ssh_config", directory / "known_hosts"

    def invoke(argv: list[str]) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise fail("SSH 접속 실패")
        try:
            result = runner(argv, timeout=min(remaining, 20))
        except (OSError, subprocess.SubprocessError, TimeoutError):
            raise fail("SSH 접속 실패") from None
        if result.returncode or time.monotonic() >= deadline:
            raise fail("SSH 접속 실패")
        return result.stdout

    try:
        blocks = _block("ddak-target", config, config.key_path, known)
        keys = ""
        _private_write(known, "")
        if config.jump_host:
            jump = config.jump_host
            blocks += "    ProxyJump ddak-jump\n" + _block(
                "ddak-jump", jump, config.key_path, known
            )
            raw = invoke(["ssh-keyscan", "-T", "5", "-p", str(jump.port), jump.host])
            keys = _pinned(raw, "ddak-jump", jump.host_key_fingerprint)
            _private_write(known, keys)
        _private_write(cfg, blocks)
        scan = ["ssh-keyscan", "-T", "5", "-p", str(config.port), config.host]
        if config.jump_host:
            scan = [ssh, "-F", str(cfg), "--", "ddak-jump", *scan]
        keys += _pinned(invoke(scan), "ddak-target", config.host_key_fingerprint)
        _private_write(known, keys)
        invoke([ssh, "-F", str(cfg), "--", "ddak-target", "true"])
        _private_write(
            directory / "ssh",
            f'#!/bin/sh\nexec {shlex.quote(ssh)} -F {shlex.quote(str(cfg))} "$@"\n',
            0o700,
        )
        prefix = [
            "/usr/bin/env",
            "-u",
            "DOCKER_HOST",
            "-u",
            "DOCKER_CONTEXT",
            "PATH=" + str(directory) + os.pathsep + os.environ.get("PATH", os.defpath),
        ]

        def remote_runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
            return runner([*prefix, *argv], timeout=timeout)

        yield remote_runner
    finally:
        shutil.rmtree(directory)
