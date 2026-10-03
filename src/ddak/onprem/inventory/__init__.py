"""onprem/inventory: 인벤토리(tier별 주소) 읽기. 담당 정준우(O1, O2 승계).

공개 함수: load_inventory. 결과는 RunContext.platform["onprem"] 모양이다(모양은
ddak.onprem.deploy provider docstring의 인벤토리 계약). 값·시크릿은 넣지 않는다.
이 모듈은 컨텍스트를 만들지 않는다(flow가 결과를 넣는다). AI import 금지(계약 1).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError, field_validator

from ddak.core.contracts.base import TIER_PATTERN
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.onprem.deploy import InventoryConfig, TierConfig

__all__ = ["load_inventory"]

_TIER = re.compile(TIER_PATTERN)


class _Inventory(InventoryConfig):
    # tier/SSH/DB 검증은 배포기와 같은 모델. 파일 로더의 unix socket 정책만 추가한다.
    @field_validator("docker_host")
    @classmethod
    def local_only(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("unix://"):
            raise ValueError("docker_host는 로컬 unix:// 소켓만 허용(ssh/tcp 미지원)")
        return value

    @field_validator("tiers")
    @classmethod
    def tier_names(cls, value: dict[str, TierConfig]) -> dict[str, TierConfig]:
        for name in value:
            if not _TIER.fullmatch(name):
                raise ValueError(f"tier 이름 형식 위반: {name!r}")
        return value


def load_inventory(path: Path) -> dict[str, Any]:
    """배포기와 동일한 VM/tier/DB 계약으로 검증한 공개 인벤토리를 돌려준다."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, f"인벤토리를 읽을 수 없다: {path.name} ({type(exc).__name__})"
        ) from exc
    if not isinstance(raw, dict):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "인벤토리 최상위는 매핑이어야 한다")
    try:
        return _Inventory.model_validate(raw).model_dump(mode="json")
    except ValidationError as exc:
        # 값은 시크릿일 수 있어 위치와 사유만 낸다.
        detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors()[:5])
        raise DdakToolError(ErrorCode.CONFIG_INVALID, f"인벤토리 검증 실패: {detail}") from exc
