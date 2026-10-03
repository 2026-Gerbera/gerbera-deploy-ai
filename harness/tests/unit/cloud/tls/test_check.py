from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from ddak.cloud.tls import check_tls
from ddak.core.contracts.errors import DdakToolError


def test_tls_pass_and_mismatches():
    now = datetime.now(UTC)
    platform = dict(
        alb_arn="alb", certificate_arn="cert", https_listener_arn="https", http_listener_arn="http"
    )
    certificate = dict(
        Status="ISSUED",
        NotBefore=now - timedelta(days=1),
        NotAfter=now + timedelta(days=1),
        SubjectAlternativeNames=["*.example.com"],
        InUseBy=["alb"],
    )
    listeners = [
        dict(
            ListenerArn="https",
            LoadBalancerArn="alb",
            Port=443,
            Protocol="HTTPS",
            SslPolicy="ELBSecurityPolicy-TLS13-1-2-2021-06",
            Certificates=[{"CertificateArn": "cert"}],
        ),
        dict(
            ListenerArn="http",
            LoadBalancerArn="alb",
            Port=80,
            Protocol="HTTP",
            DefaultActions=[
                dict(
                    Type="redirect",
                    RedirectConfig=dict(
                        Protocol="HTTPS", Port="443", StatusCode="HTTP_301", Host="#{host}"
                    ),
                )
            ],
        ),
    ]
    acm = Mock(describe_certificate=Mock(return_value={"Certificate": certificate}))
    elb = Mock(describe_listeners=Mock(return_value={"Listeners": listeners}))
    rules = {"Rules": [{"IsDefault": True}]}
    elb.describe_rules.return_value = rules

    def call(domain="app.example.com"):
        return check_tls(
            domain=domain, platform=platform, acm=acm, elbv2=elb, now=now, probe=lambda *_: True
        )

    assert call().passed
    rules["Rules"].append({"IsDefault": False, "Actions": [{"Type": "forward"}]})
    assert not call().passed
    rules["Rules"].pop()
    rules["NextMarker"] = "more-rules"
    assert not call().passed
    del rules["NextMarker"]
    assert call().passed
    assert not call("sub.app.example.com").passed
    listeners[0]["LoadBalancerArn"] = "wrong-alb"
    assert not call().passed
    listeners[0]["LoadBalancerArn"] = "alb"
    listeners[1]["DefaultActions"][0]["RedirectConfig"]["Host"] = "other.example.com"
    assert not call().passed
    redirect = listeners[1]["DefaultActions"][0]["RedirectConfig"]
    redirect["Host"] = "#{host}"
    for field, wrong, preserved in (
        ("Path", "/other", "/#{path}"),
        ("Query", "lost=1", "#{query}"),
    ):
        redirect[field] = wrong
        assert not call().passed
        redirect[field] = preserved
        assert call().passed
    certificate["Status"] = "PENDING_VALIDATION"
    assert not call().passed
    assert {c[0] for c in acm.mock_calls} == {"describe_certificate"}
    assert {c[0] for c in elb.mock_calls} == {"describe_listeners", "describe_rules"}


def test_tls_cli_error_is_redacted():
    acm = Mock(describe_certificate=Mock(side_effect=RuntimeError("do-not-leak")))
    with pytest.raises(DdakToolError) as error:
        check_tls(
            domain="app.example.com",
            platform=dict(
                alb_arn="a", certificate_arn="c", https_listener_arn="s", http_listener_arn="h"
            ),
            acm=acm,
            elbv2=Mock(),
        )
    assert "do-not-leak" not in str(error.value)


def test_fake_context_never_creates_aws_session(monkeypatch):
    from ddak.cloud.tls import ensure_tls
    from ddak.core.contracts.context import RunContext

    client = Mock()
    monkeypatch.setattr("ddak.cloud.tls.check.boto3.Session", client)
    with pytest.raises(DdakToolError, match="가짜 모드"):
        ensure_tls(
            "check",
            RunContext(
                "run-1",
                cloud_domain="app.example.com",
                platform={
                    "alb_arn": "a",
                    "certificate_arn": "c",
                    "https_listener_arn": "s",
                    "http_listener_arn": "h",
                },
            ),
        )
    client.assert_not_called()


def test_tls_deadline_stops_before_second_lookup(monkeypatch):
    monkeypatch.setattr("ddak.cloud.tls.check.time.monotonic", Mock(side_effect=[1, 3]))
    acm = Mock(describe_certificate=Mock(return_value={"Certificate": {}}))
    elb = Mock()
    result = check_tls(
        domain="app.example.com",
        platform=dict(
            alb_arn="a", certificate_arn="c", https_listener_arn="s", http_listener_arn="h"
        ),
        acm=acm,
        elbv2=elb,
        deadline=2,
    )
    assert result.passed is False
    acm.describe_certificate.assert_called_once()
    elb.describe_listeners.assert_not_called()


@pytest.mark.parametrize(
    "failure", [None, OSError("fixture"), __import__("ssl").SSLError("fixture")]
)
def test_real_probe_uses_443_verified_tls_and_closes(monkeypatch, failure):
    import ssl

    from ddak.cloud.tls.check import _probe_https as probe_https

    connection = Mock()
    connection.connect.side_effect = failure
    connection.getresponse.return_value.status = 503  # TLS는 준비됐고 앱은 별도 health에서 확인
    factory = Mock(return_value=connection)
    monkeypatch.setattr("ddak.cloud.tls.check.http.client.HTTPSConnection", factory)
    assert probe_https("app.example.test") is (failure is None)
    assert factory.call_args.args == ("app.example.test",)
    assert factory.call_args.kwargs["port"] == 443
    context = factory.call_args.kwargs["context"]
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    connection.close.assert_called_once()


def test_probe_clamps_large_deadline_and_expiry_is_false(monkeypatch):
    import subprocess
    import time

    from ddak.cloud.tls import probe_https

    runner = Mock(return_value=subprocess.CompletedProcess([], 0, b"1"))
    monkeypatch.setattr("ddak.cloud.tls.check.subprocess.run", runner)
    start = time.monotonic()
    assert probe_https("app.example.test", start + 2700)
    assert 0 < runner.call_args.kwargs["timeout"] <= 10
    assert float(runner.call_args.args[0][-1]) <= start + 10.1
    runner.reset_mock()
    assert not probe_https("app.example.test", start - 1)
    runner.assert_not_called()


def test_dns_hang_uses_subprocess_timeout_and_reports_failure(monkeypatch):
    import subprocess
    import time

    from ddak.cloud.tls import probe_https

    # subprocess.run의 timeout은 자식 회수를 담당한다. 여기서는 외부 실행 없이
    # 제품이 DNS를 포함한 전체 작업에 제한 시간을 적용하고 실패를 반환하는지 검사한다.
    runner = Mock(side_effect=subprocess.TimeoutExpired("fixture-probe", 1))
    monkeypatch.setattr("ddak.cloud.tls.check.subprocess.run", runner)
    start = time.monotonic()
    assert not probe_https("app.example.test", start + 1)
    assert 0 < runner.call_args.kwargs["timeout"] <= 1
    assert "_probe_https" in runner.call_args.args[0][2]
    assert time.monotonic() - start < 2
