"""C3 클라우드 검증의 안전한 로컬 경로."""

from __future__ import annotations

import pytest

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
