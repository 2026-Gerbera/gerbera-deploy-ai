"""plan/planner 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.core.contracts.context import RunContext
from ddak.plan.planner import generate_plan


def test_planner_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="plan/planner 미구현: 담당 김준석"):
        generate_plan(None, RunContext("run-1"))
