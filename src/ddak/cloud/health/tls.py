"""클라우드 TLS 검증. 서비스 변경 없이 DNS·소켓·AWS 읽기 API만 사용한다."""

from __future__ import annotations

import http.client
import socket
import ssl
import time
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from ddak.cloud.health.aws import client, cloud_platform, remaining, required
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.verify_tls import TlsCheck, VerifyTlsInput, VerifyTlsOutput

_REQUIRED = frozenset({"V1", "V2", "V3", "V4", "V5", "V6", "V7"})
_TLS_POLICIES = frozenset(
    {
        "ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09",
        "ELBSecurityPolicy-TLS13-1-2-2021-06",
    }
)


def _check(
    id_: Literal["V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9"],
    name: str,
    ok: bool | None,
    detail: str,
) -> TlsCheck:
    status = "inconclusive" if ok is None else "pass" if ok else "fail"
    return TlsCheck(id=id_, name=name, status=status, detail=detail)


def _addresses(host: str) -> set[str]:
    return {row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}


def _certificate(alb_dns: str, domain: str, timeout: float) -> tuple[dict[str, Any], str]:
    context = ssl.create_default_context()
    with (
        socket.create_connection((alb_dns, 443), timeout=timeout) as raw,
        context.wrap_socket(raw, server_hostname=domain) as tls,
    ):
        return tls.getpeercert(), tls.version() or "unknown"


def _issuer_org(cert: dict[str, Any]) -> str:
    for group in cert.get("issuer", ()):
        for key, value in group:
            if key == "organizationName":
                return str(value)
    return ""


def _days_left(cert: dict[str, Any]) -> int:
    not_after = cert.get("notAfter")
    if not isinstance(not_after, str):
        return -1
    seconds = ssl.cert_time_to_seconds(not_after)
    return int((datetime.fromtimestamp(seconds, UTC) - datetime.now(UTC)).total_seconds() / 86400)


def _http_redirect(domain: str, timeout: float) -> tuple[bool, str]:
    connection = http.client.HTTPConnection(domain, 80, timeout=timeout)
    try:
        connection.request("GET", "/", headers={"User-Agent": "ddak-verifier/1"})
        response = connection.getresponse()
        location = response.getheader("Location") or ""
        response.read(1024)
        try:
            target = urlsplit(location)
            port = target.port
        except ValueError:
            target = urlsplit("")
            port = None
        ok = (
            response.status == 301
            and target.scheme.lower() == "https"
            and target.hostname is not None
            and target.hostname.rstrip(".").lower() == domain.rstrip(".").lower()
            and port in (None, 443)
            and target.username is None
            and target.password is None
        )
        return ok, f"status={response.status}, location={'https' if location else 'none'}"
    finally:
        connection.close()


def _hsts(domain: str, timeout: float) -> tuple[bool, str]:
    connection = http.client.HTTPSConnection(
        domain, 443, timeout=timeout, context=ssl.create_default_context()
    )
    try:
        connection.request("GET", "/", headers={"User-Agent": "ddak-verifier/1"})
        response = connection.getresponse()
        hsts = response.getheader("Strict-Transport-Security") or ""
        response.read(1024)
        return response.status == 200 and bool(hsts), "앱 200 응답의 HSTS 확인"
    finally:
        connection.close()


def verify_tls(inp: VerifyTlsInput, ctx: RunContext) -> VerifyTlsOutput:
    if inp.target is not Target.CLOUD:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "verify_tls는 cloud 대상만 지원한다")
    if not ctx.cloud_domain:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "cloud_domain이 설정되지 않았다")
    started = time.monotonic()
    platform = cloud_platform(ctx)
    region = required(platform, "region")
    alb_dns = required(platform, "alb_dns_name")
    alb_arn = required(platform, "alb_arn")
    certificate_arn = required(platform, "certificate_arn")
    https_listener_arn = required(platform, "https_listener_arn")
    http_listener_arn = required(platform, "http_listener_arn")
    checks: list[TlsCheck] = []

    try:
        dns_ok = bool(_addresses(ctx.cloud_domain) & _addresses(alb_dns))
        checks.append(_check("V1", "DNS 연결", dns_ok, "도메인과 ALB 주소 교집합 확인"))
    except OSError:
        checks.append(_check("V1", "DNS 연결", False, "DNS 조회 실패"))

    cert: dict[str, Any] = {}
    tls_version = "unknown"
    try:
        cert, tls_version = _certificate(alb_dns, ctx.cloud_domain, remaining(ctx))
        checks.append(_check("V2", "인증서 체인·호스트명", True, "기본 신뢰 저장소 검증 통과"))
    except (OSError, ssl.SSLError):
        checks.append(_check("V2", "인증서 체인·호스트명", False, "TLS 검증 실패"))
    version_ok = tls_version in {"TLSv1.2", "TLSv1.3"}
    checks.append(_check("V3", "TLS 버전", version_ok, f"협상 버전 {tls_version}"))
    days = _days_left(cert)
    cert_ok = _issuer_org(cert) == "Amazon" and days >= 30
    checks.append(_check("V4", "인증서 내용", cert_ok, f"Amazon 발급, 잔여 {days}일"))

    acm = client("acm", ctx, region)
    elbv2 = client("elbv2", ctx, region)
    try:
        remote = acm.describe_certificate(CertificateArn=certificate_arn)["Certificate"]
        in_use = alb_arn in remote.get("InUseBy", [])
        acm_ok = remote.get("Status") == "ISSUED" and in_use
        checks.append(_check("V5", "ACM 교차 확인", acm_ok, "ISSUED 및 ALB 연결 확인"))
    except (KeyError, TypeError):
        checks.append(_check("V5", "ACM 교차 확인", False, "ACM 응답 형식 오류"))

    https_listener = elbv2.describe_listeners(ListenerArns=[https_listener_arn])["Listeners"][0]
    http_listener = elbv2.describe_listeners(ListenerArns=[http_listener_arn])["Listeners"][0]
    redirects = [
        action.get("RedirectConfig", {})
        for action in http_listener.get("DefaultActions", [])
        if action.get("Type") == "redirect"
    ]
    redirect_config_ok = any(
        item.get("Protocol") == "HTTPS"
        and str(item.get("Port")) == "443"
        and str(item.get("StatusCode")) == "HTTP_301"
        for item in redirects
    )
    listener_ok = (
        https_listener.get("Port") == 443
        and https_listener.get("SslPolicy") in _TLS_POLICIES
        and http_listener.get("Port") == 80
        and redirect_config_ok
    )
    checks.append(_check("V6", "ALB 리스너", listener_ok, "443 정책 및 80→301 확인"))

    try:
        redirect_ok, detail = _http_redirect(ctx.cloud_domain, remaining(ctx))
        checks.append(_check("V7", "HTTP 리다이렉트", redirect_ok, detail))
    except OSError:
        checks.append(_check("V7", "HTTP 리다이렉트", False, "HTTP 연결 실패"))
    checks.append(
        _check("V8", "구형 TLS 거부", None, "로컬 OpenSSL 정책에 따라 별도 리허설에서 확인")
    )
    try:
        hsts_ok, detail = _hsts(ctx.cloud_domain, remaining(ctx))
        checks.append(_check("V9", "HSTS", hsts_ok, detail))
    except OSError:
        checks.append(_check("V9", "HSTS", False, "HTTPS 앱 응답 확인 실패"))

    passed = all(check.status == "pass" for check in checks if check.id in _REQUIRED)
    return VerifyTlsOutput(
        run_id=inp.run_id,
        target=inp.target,
        passed=passed,
        checks=checks,
        elapsed_s=time.monotonic() - started,
    )
