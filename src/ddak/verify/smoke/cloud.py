"""클라우드 어댑터(target=cloud). 관리 페이지에 입력한 도메인으로 HTTPS 요청(인증서 검증)."""

from __future__ import annotations

from typing import Literal

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.domain import require_cloud_domain
from ddak.verify.smoke.logic import HttpClient, UrlClient


class CloudSmokeAdapter:
    name = "cloud"
    source: Literal["live", "fixture"] = "live"

    @property
    def target(self) -> Target:
        return Target.CLOUD

    def client(self, ctx: RunContext) -> HttpClient:
        domain = require_cloud_domain(ctx.cloud_domain)
        return UrlClient(f"https://{domain}")
