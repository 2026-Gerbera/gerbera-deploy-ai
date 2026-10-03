"""onprem/provision: 온프렘 서버·컨테이너 준비, 앱 DB·계정. 담당 정준우(O1, O2 승계).

공개 이름: prepare_host(점검만, 변경 없음), HostCheck, Check, ensure_app_database(미구현).
AI import 금지(import-linter 계약 1).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext

__all__ = ["Check", "HostCheck", "ensure_app_database", "prepare_host"]

_TODO = "onprem/provision 미구현: 담당 정준우(O1)"
_TIMEOUT = 15.0
_Run = Callable[[list[str], float], int]


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class HostCheck:
    ok: bool
    checks: tuple[Check, ...]


def _run(argv: list[str], timeout: float) -> int:
    try:
        return subprocess.run(
            argv, capture_output=True, timeout=timeout, shell=False, check=False
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def _networks(ctx: RunContext) -> list[str]:
    tiers = (ctx.platform.get("onprem") or {}).get("tiers") or {}
    names = {t.get("network", "bridge") for t in tiers.values() if isinstance(t, dict)}
    return sorted(n for n in names if n != "bridge")


def prepare_host(ctx: RunContext, *, run: _Run = _run) -> HostCheck:
    """Docker 엔진과 인벤토리가 쓰는 네트워크 존재를 확인만 한다(생성·삭제 없음)."""
    if ctx.adapter_mode == AdapterMode.FAKE:
        return HostCheck(True, (Check("docker_info", True, "fake"),))
    host = (ctx.platform.get("onprem") or {}).get("docker_host")
    base = ["docker", *(["--host", host] if host else [])]
    checks = [Check("docker_info", run([*base, "info"], _TIMEOUT) == 0)]
    checks += [
        Check(f"network:{n}", run([*base, "network", "inspect", n], _TIMEOUT) == 0)
        for n in _networks(ctx)
    ]
    return HostCheck(all(c.ok for c in checks), tuple(checks))


def ensure_app_database(ctx: RunContext) -> object:
    """앱 DB·최소 권한 계정 준비(값은 env 파일에만). 결정 대기로 미구현(플랜 06 충돌 기록)."""
    raise NotImplementedError(_TODO)
