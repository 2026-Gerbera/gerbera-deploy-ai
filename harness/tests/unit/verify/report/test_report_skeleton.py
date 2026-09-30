"""verify/report 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.core.contracts.context import RunContext
from ddak.verify.report import post_report


def test_report_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="verify/report 미구현: 담당 양서윤"):
        post_report(None, RunContext("run-1"))
