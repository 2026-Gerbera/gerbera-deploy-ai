"""클라우드 어댑터(target=cloud). 관리 페이지에 입력한 도메인으로 HTTPS 요청(인증서 검증)."""

from __future__ import annotations

import re
from typing import Literal

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.verify.smoke.logic import HttpClient, UrlClient

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_DOMAIN = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})+$")


class CloudSmokeAdapter:
    name = "cloud"
    source: Literal["live", "fixture"] = "live"

    @property
    def target(self) -> Target:
        return Target.CLOUD

    def client(self, ctx: RunContext) -> HttpClient:
        domain = ctx.cloud_domain or ""
        if not _DOMAIN.fullmatch(domain):
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "클라우드 도메인이 설정되지 않았거나 형식 오류"
            )
        return UrlClient(f"https://{domain}")
