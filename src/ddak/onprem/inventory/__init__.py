"""onprem/inventory: 인벤토리(tier별 주소) 읽기. 담당 김준석(O2).

공개 함수: load_inventory. 결과는 RunContext.platform["onprem"] 모양이다(모양은
ddak.onprem.deploy provider docstring의 인벤토리 계약). 값·시크릿은 넣지 않는다.
AI import 금지(import-linter 계약 1). 빈 구현이다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_TODO = "onprem/inventory 미구현: 담당 김준석"


def load_inventory(path: Path) -> dict[str, Any]:
    """platform.onprem.yaml을 읽어 {"docker_host": ..., "tiers": {...}}를 돌려준다."""
    raise NotImplementedError(_TODO)
