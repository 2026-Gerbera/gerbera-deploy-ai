"""verify/diagnose 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.core.contracts.context import RunContext
from ddak.verify.diagnose import diagnose_parity_gap


def test_diagnose_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="verify/diagnose 미구현: 담당 장민영"):
        diagnose_parity_gap(None, RunContext("run-1"))
