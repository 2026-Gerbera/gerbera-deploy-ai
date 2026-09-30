"""plan/analyze 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.core.contracts.context import RunContext
from ddak.plan.analyze import analyze_project


def test_analyze_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="plan/analyze 미구현: 담당 김준석"):
        analyze_project(None, RunContext("run-1"))
