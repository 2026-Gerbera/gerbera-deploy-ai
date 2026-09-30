"""plan/validate 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.core.contracts.context import RunContext
from ddak.plan.validate import validate_plan


def test_validate_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="plan/validate 미구현: 담당 김준석"):
        validate_plan(None, RunContext("run-1"))
