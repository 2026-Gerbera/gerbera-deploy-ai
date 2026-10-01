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
        return check_tls(domain=domain, platform=platform, acm=acm, elbv2=elb, now=now)

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
    with pytest.raises(DdakToolError, match="ADAPTER_TIMEOUT"):
        check_tls(
            domain="app.example.com",
            platform=dict(
                alb_arn="a", certificate_arn="c", https_listener_arn="s", http_listener_arn="h"
            ),
            acm=acm,
            elbv2=elb,
            deadline=2,
        )
    acm.describe_certificate.assert_called_once()
    elb.describe_listeners.assert_not_called()
