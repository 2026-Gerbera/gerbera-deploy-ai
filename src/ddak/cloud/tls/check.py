"""ACM/ALB 설정과 실제 HTTPS 443을 확인한다. HSTS 검증은 C3 verify_tls 담당."""

from __future__ import annotations

import http.client
import re
import ssl
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

import boto3
from botocore.config import Config

from ddak.cd.interface import ProviderResult
from ddak.core.aws_credentials import checked_session
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode

TLS_POLICIES = {"ELBSecurityPolicy-TLS13-1-2-2021-06", "ELBSecurityPolicy-TLS13-1-2-Res-2021-06"}


def _result(passed: bool, detail: str) -> ProviderResult:
    return ProviderResult(provider="aws", function="ensure_tls", passed=passed, detail=detail)


def _covers(name: str, domain: str) -> bool:
    name, domain = name.lower().rstrip("."), domain.lower().rstrip(".")
    return name == domain or (
        name.startswith("*.") and domain.count(".") == name.count(".") and domain.endswith(name[1:])
    )


def probe_https(domain: str, deadline: float | None = None) -> bool:
    """DNS까지 같은 10초 예산에 포함한다. 시간 초과 시 자식 종료·회수 뒤 불합격."""
    now = time.monotonic()
    end = min(now + 10, deadline) if deadline is not None else now + 10
    if end <= now:
        return False
    code = (
        "import os,sys; from ddak.cloud.tls.check import _probe_https; "
        "os.write(1, b'1' if _probe_https(sys.argv[1], float(sys.argv[2])) else b'0')"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, domain, str(end)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=max(0, end - time.monotonic()),
        )
        return result.returncode == 0 and result.stdout == b"1" and time.monotonic() <= end
    except (OSError, subprocess.TimeoutExpired):
        return False


def _probe_https(domain: str, deadline: float | None = None) -> bool:
    """인증서 체인·호스트명 검증을 켠 실제 443 연결. 앱 상태는 후속 health가 검사한다."""
    end = deadline if deadline is not None else time.monotonic() + 10
    remaining = end - time.monotonic()
    if remaining <= 0:
        return False
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    connection = http.client.HTTPSConnection(domain, port=443, timeout=remaining, context=context)
    try:
        connection.connect()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return False
        if connection.sock is None:
            return False
        connection.sock.settimeout(remaining)
        connection.request("HEAD", "/")
        response = connection.getresponse()
        return 100 <= response.status <= 599
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def check_tls(
    *,
    domain: str,
    platform: Mapping[str, Any],
    acm: Any,
    elbv2: Any,
    now: datetime | None = None,
    deadline: float | None = None,
    probe: Callable[[str, float | None], bool] | None = None,
) -> ProviderResult:
    def within_deadline() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("TLS 확인 제한 시간 초과")

    names = ("alb_arn", "certificate_arn", "https_listener_arn", "http_listener_arn")
    if not all(isinstance(platform.get(k), str) and platform[k] for k in names):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "TLS 확인에 필요한 인프라 출력이 없다")
    if not re.fullmatch(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", domain):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "클라우드 도메인 형식 오류")
    try:
        now = now or datetime.now(UTC)
        within_deadline()
        cert = acm.describe_certificate(CertificateArn=platform["certificate_arn"])["Certificate"]
        within_deadline()
        if (
            cert.get("Status") != "ISSUED"
            or not cert.get("NotBefore")
            or not cert.get("NotAfter")
            or not cert["NotBefore"] <= now < cert["NotAfter"]
        ):
            return _result(False, "ACM 인증서 발급 상태 또는 유효기간 불일치")
        if not any(_covers(name, domain) for name in cert.get("SubjectAlternativeNames", [])):
            return _result(False, "ACM 인증서 도메인 불일치")
        if platform["alb_arn"] not in cert.get("InUseBy", []):
            return _result(False, "ACM 인증서가 대상 ALB에 연결되지 않음")
        listeners = elbv2.describe_listeners(
            ListenerArns=[platform["https_listener_arn"], platform["http_listener_arn"]]
        )["Listeners"]
        within_deadline()
        by_arn = {listener["ListenerArn"]: listener for listener in listeners}
        https = by_arn.get(platform["https_listener_arn"], {})
        http = by_arn.get(platform["http_listener_arn"], {})
        if (
            https.get("LoadBalancerArn") != platform["alb_arn"]
            or https.get("Port") != 443
            or https.get("Protocol") != "HTTPS"
            or https.get("SslPolicy") not in TLS_POLICIES
            or platform["certificate_arn"]
            not in {c["CertificateArn"] for c in https.get("Certificates", [])}
        ):
            return _result(False, "443 리스너 인증서·TLS 정책·ALB 불일치")
        actions = http.get("DefaultActions", [])
        redirect = (
            actions[0].get("RedirectConfig", {})
            if len(actions) == 1 and actions[0].get("Type") == "redirect"
            else {}
        )
        if (
            http.get("LoadBalancerArn") != platform["alb_arn"]
            or http.get("Port") != 80
            or http.get("Protocol") != "HTTP"
            or redirect.get("Protocol") != "HTTPS"
            or redirect.get("Port") != "443"
            or redirect.get("StatusCode") != "HTTP_301"
            or redirect.get("Host") not in ("#{host}", domain)
            or redirect.get("Path", "/#{path}") != "/#{path}"
            or redirect.get("Query", "#{query}") != "#{query}"
        ):
            return _result(False, "80 리스너 HTTPS 리다이렉트 불일치")
        rules = elbv2.describe_rules(ListenerArn=platform["http_listener_arn"])
        within_deadline()
        # 현재 플랫폼은 HTTP의 기본 redirect만 지원한다. 우선 규칙으로 우회하지 못한다.
        if (
            rules.get("NextMarker")
            or len(rules.get("Rules", [])) != 1
            or rules["Rules"][0].get("IsDefault") is not True
        ):
            return _result(False, "80 리스너 추가 규칙 또는 규칙 조회 불일치")
        within_deadline()
        probe_end = (
            min(time.monotonic() + 10, deadline) if deadline is not None else time.monotonic() + 10
        )
        if not (probe or probe_https)(domain, probe_end):
            return _result(False, "실제 443 연결·인증서 확인 실패")
        return _result(True, "ACM·리스너 설정과 실제 HTTPS 443 응답 확인")
    except TimeoutError:
        return _result(False, "TLS 확인 제한 시간 초과")
    except DdakToolError:
        raise
    except Exception:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "AWS TLS 상태를 조회할 수 없다") from None


def ensure_tls(mode: Literal["check", "apply"], ctx: RunContext) -> ProviderResult:
    if mode != "check":
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "TLS 변경은 플랫폼 Terraform만 수행한다")
    if not ctx.cloud_domain:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "cloud_domain이 설정되지 않았다")
    if ctx.adapter_mode != AdapterMode.REAL:
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED, "가짜 모드에서는 AWS TLS를 조회하지 않는다"
        )
    platform = ctx.platform.get("cloud", ctx.platform)
    if not isinstance(platform, Mapping):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "클라우드 인프라 출력 형식 오류")
    if not all(
        platform.get(k)
        for k in ("alb_arn", "certificate_arn", "https_listener_arn", "http_listener_arn")
    ):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "TLS 확인에 필요한 인프라 출력이 없다")
    remaining = min(10.0, ctx.deadline - time.monotonic()) if ctx.deadline else 10.0
    if remaining <= 0:
        return _result(False, "TLS 확인 제한 시간 초과")
    try:
        config = Config(
            # STS와 세 번의 조회 각각의 connect/read에 전체 예산을 나눠 쓴다.
            connect_timeout=remaining / 8,
            read_timeout=remaining / 8,
            retries={"total_max_attempts": 1},
        )
        session = checked_session(
            ctx.project_settings,
            region_name="ap-northeast-2",
            config=config,
            session_factory=boto3.Session,
        )
        return check_tls(
            domain=ctx.cloud_domain,
            platform=platform,
            acm=session.client("acm", config=config),
            elbv2=session.client("elbv2", config=config),
            deadline=ctx.deadline or None,
        )
    except DdakToolError:
        raise
    except Exception:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "AWS 검증 세션을 준비할 수 없다") from None
