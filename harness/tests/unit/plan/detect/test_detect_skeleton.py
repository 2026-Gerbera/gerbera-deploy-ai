"""plan/detect 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.core.contracts.context import RunContext
from ddak.plan.detect import detect_changed_tiers


def test_detect_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="plan/detect 미구현: 담당 김준석"):
        detect_changed_tiers(None, RunContext("run-1"))
