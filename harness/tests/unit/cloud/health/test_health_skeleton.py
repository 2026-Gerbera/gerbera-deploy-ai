"""C3 클라우드 검증의 안전한 로컬 경로."""

from __future__ import annotations

import pytest

import ddak.cloud.health.tls as tls
from ddak.cloud.health import health_check
from ddak.cloud.health.fake import fake_verify_tls
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError


def test_health_requires_cloud_domain_before_aws_call() -> None:
    with pytest.raises(DdakToolError, match="cloud_domain"):
        health_check(RunContext("run-1"))


def test_fake_tls_is_deterministic_and_complete() -> None:
    result = fake_verify_tls("run-1")
    assert result.passed is True
    assert [check.id for check in result.checks] == [f"V{i}" for i in range(1, 10)]


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("https://app.example.com/", True),
        ("https://app.example.com:443/", True),
        ("https://app.example.com:444/", False),
        ("https://other.example.com/", False),
    ],
)
def test_http_redirect_accepts_only_same_host_https_default_port(
    monkeypatch: pytest.MonkeyPatch, location: str, expected: bool
) -> None:
    class HttpResponse:
        status = 301

        @staticmethod
        def getheader(name: str) -> str | None:
            return location if name == "Location" else None

        @staticmethod
        def read(limit: int) -> bytes:
            del limit
            return b""

    class Connection:
        def request(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        @staticmethod
        def getresponse() -> HttpResponse:
            return HttpResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr(tls.http.client, "HTTPConnection", lambda *args, **kwargs: Connection())

    passed, _ = tls._http_redirect("app.example.com", 1)

    assert passed is expected
