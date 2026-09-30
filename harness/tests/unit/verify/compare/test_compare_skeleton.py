"""verify/compare 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.core.contracts.context import RunContext
from ddak.verify.compare import compare_env_results


def test_compare_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="verify/compare 미구현: 담당 장민영"):
        compare_env_results(None, RunContext("run-1"))
