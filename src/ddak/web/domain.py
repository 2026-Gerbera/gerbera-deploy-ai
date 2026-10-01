"""사람이 입력한 클라우드 도메인 설정 검증."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from ddak.core.contracts.errors import DdakToolError, ErrorCode

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_BLOCKED_SUFFIXES = ("amazonaws.com", "cloudfront.net", "elasticbeanstalk.com")


@dataclass(frozen=True)
class DomainSettings:
    cloud_domain: str
    dns_mode: str
    hosted_zone_id: str | None


def normalize_domain(raw: str) -> str:
    value = raw.strip().rstrip(".")
    if not value or "://" in value or "/" in value or ":" in value or value.startswith("*."):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "도메인 이름만 입력하세요")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "IP 주소는 도메인으로 사용할 수 없습니다")
    try:
        value = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "도메인 IDNA 변환 실패") from exc
    if len(value) > 253 or any(not _LABEL.fullmatch(label) for label in value.split(".")):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "도메인 형식이 올바르지 않습니다")
    if len(value.split(".")) < 3:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "apex 대신 서브도메인을 입력하세요")
    if any(value == suffix or value.endswith(f".{suffix}") for suffix in _BLOCKED_SUFFIXES):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "AWS 기본 도메인은 사용할 수 없습니다")
    return value


def validate_domain_settings(
    cloud_domain: str, dns_mode: str, hosted_zone_id: str | None
) -> DomainSettings:
    if dns_mode not in {"route53", "external"}:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "dns_mode는 route53 또는 external입니다")
    zone = (hosted_zone_id or "").strip() or None
    if dns_mode == "route53" and zone is None:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "Route 53 hosted zone ID가 필요합니다")
    if zone and not re.fullmatch(r"Z[A-Z0-9]{5,31}", zone):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "hosted zone ID 형식이 올바르지 않습니다")
    return DomainSettings(normalize_domain(cloud_domain), dns_mode, zone)
