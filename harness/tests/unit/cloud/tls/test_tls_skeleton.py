"""TLS 공통 진입점은 설정/미확정 변경을 명시적으로 거절한다."""

import pytest

from ddak.cloud.tls import ensure_tls
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError


def test_tls_missing_context_fails_before_aws():
    with pytest.raises(DdakToolError, match="CONFIG_INVALID"):
        ensure_tls("check", RunContext("run-1"))


def test_tls_apply_not_enabled():
    with pytest.raises(DdakToolError, match="PRECONDITION_FAILED"):
        ensure_tls("apply", RunContext("run-1"))
