"""onprem/provision 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.core.contracts.context import RunContext
from ddak.onprem.provision import ensure_app_database, prepare_host


def test_provision_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="onprem/provision 미구현: 담당 김준석"):
        prepare_host(RunContext("run-1"))
    with pytest.raises(NotImplementedError, match="onprem/provision 미구현: 담당 김준석"):
        ensure_app_database(RunContext("run-1"))
