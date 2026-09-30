"""onprem/inventory 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

from pathlib import Path

import pytest

from ddak.onprem.inventory import load_inventory


def test_inventory_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="onprem/inventory 미구현: 담당 김준석"):
        load_inventory(Path("platform.onprem.yaml"))
