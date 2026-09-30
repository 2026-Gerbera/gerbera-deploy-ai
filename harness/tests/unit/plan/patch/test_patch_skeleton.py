"""plan/patch 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.core.contracts.context import RunContext
from ddak.plan.patch import patch_config, patch_db_access, patch_storage


def test_patch_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="plan/patch 미구현: 담당 장민영"):
        patch_config(None, RunContext("run-1"))
    with pytest.raises(NotImplementedError, match="plan/patch 미구현: 담당 장민영"):
        patch_db_access(None, RunContext("run-1"))
    with pytest.raises(NotImplementedError, match="plan/patch 미구현: 담당 장민영"):
        patch_storage(None, RunContext("run-1"))
