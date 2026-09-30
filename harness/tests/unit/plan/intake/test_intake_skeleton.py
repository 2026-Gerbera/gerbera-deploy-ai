"""plan/intake 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.core.contracts.context import RunContext
from ddak.plan.intake import receive_deploy_request


def test_intake_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="plan/intake 미구현: 담당 김준석"):
        receive_deploy_request(None, RunContext("run-1"))
