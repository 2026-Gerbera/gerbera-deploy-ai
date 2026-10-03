"""온프렘 어댑터(target=local). 주소는 실행 컨텍스트의 온프렘 인벤토리에서 읽는다."""

from __future__ import annotations

from typing import Any, Literal

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.verify.smoke.logic import HttpClient, UrlClient


def local_base_url(ctx: RunContext) -> str:
    """인벤토리 public_url, 없으면 tier 공개 설정의 APP_BASE_URL(같은 값이 앱에 주입된다)."""
    onprem: Any = ctx.platform.get("onprem") or {}
    url = onprem.get("public_url") if isinstance(onprem, dict) else None
    if not url and isinstance(onprem, dict):
        for tier in (onprem.get("tiers") or {}).values():
            public_env = (tier or {}).get("public_env") or {}
            url = public_env.get("APP_BASE_URL")
            if url:
                break
    if not isinstance(url, str) or not url:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "온프레미스 공개 주소가 인벤토리에 없다")
    return url


class LocalSmokeAdapter:
    name = "local"
    source: Literal["live", "fixture"] = "live"

    @property
    def target(self) -> Target:
        return Target.LOCAL

    def client(self, ctx: RunContext) -> HttpClient:
        return UrlClient(local_base_url(ctx))
