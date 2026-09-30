"""cloud/tls 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.cloud.tls import ensure_tls
from ddak.core.contracts.context import RunContext


def test_tls_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="cloud/tls 미구현: 담당 유상준"):
        ensure_tls("check", RunContext("run-1"))
