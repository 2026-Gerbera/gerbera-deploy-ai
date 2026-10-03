"""실행 시 사용하는 클라우드 도메인의 공통 형식 검증."""

from __future__ import annotations

import re

from ddak.core.contracts.errors import DdakToolError, ErrorCode

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_DOMAIN = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})+$")


def require_cloud_domain(value: str | None) -> str:
    """스모크와 클라우드 헬스가 공유하는 호스트명 계약을 적용한다."""
    domain = value or ""
    if not _DOMAIN.fullmatch(domain):
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, "클라우드 도메인이 설정되지 않았거나 형식 오류"
        )
    return domain
