"""verify/smoke 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.core.contracts.context import RunContext
from ddak.verify.smoke import smoke_test


def test_smoke_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="verify/smoke 미구현: 담당 장민영"):
        smoke_test(None, RunContext("run-1"))
