"""cloud/health 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.cloud.health import health_check, verify_tls
from ddak.core.contracts.context import RunContext


def test_health_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="cloud/health 미구현: 담당 양서윤"):
        health_check(RunContext("run-1"))
    with pytest.raises(NotImplementedError, match="cloud/health 미구현: 담당 양서윤"):
        verify_tls(None, RunContext("run-1"))
