"""접수(fetch) 정책. 불변 dataclass. 토큰은 repr·str·로그에 나오지 않는다(SecretStr)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import SecretStr

from ddak.core.contracts.errors import DdakToolError, ErrorCode

__all__ = ["FetchPolicy"]


def _positive(env: Mapping[str, str], key: str, default: float, *, integer: bool = True) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, f"{key}는 양수여야 한다")
    return value


@dataclass(frozen=True)
class FetchPolicy:
    allowed_schemes: tuple[str, ...] = ("https",)
    allowed_hosts: tuple[str, ...] | None = ("github.com",)  # None = 제한 없음(테스트 전용)
    timeout_s: float = 120.0
    max_bytes: int = 200 * 1024 * 1024
    max_files: int = 20000
    retries: int = 2
    cache_ttl_s: float = 3600.0
    token: SecretStr | None = field(default=None, repr=False)
    credentials: tuple[Path, str, str] | None = field(default=None, repr=False)
    root: Path = Path("var/sources")  # 아래에 cache/ 와 runs/

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> FetchPolicy:
        env = os.environ if environ is None else environ
        hosts = tuple(h.strip().lower() for h in env.get("DDAK_GIT_ALLOWED_HOSTS", "").split(","))
        hosts = tuple(h for h in hosts if h) or ("github.com",)
        token = env.get("DDAK_GITHUB_TOKEN") or None
        return cls(
            allowed_hosts=hosts,
            timeout_s=_positive(env, "DDAK_GIT_TIMEOUT_S", 120.0, integer=False),
            max_bytes=int(_positive(env, "DDAK_GIT_MAX_MB", 200)) * 1024 * 1024,
            max_files=int(_positive(env, "DDAK_GIT_MAX_FILES", 20000)),
            cache_ttl_s=_positive(env, "DDAK_GIT_CACHE_TTL_S", 3600.0, integer=False),
            token=SecretStr(token) if token else None,
            root=Path(env.get("DDAK_SOURCES_DIR") or "var/sources"),
        )
