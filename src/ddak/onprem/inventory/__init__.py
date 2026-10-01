"""onprem/inventory: 인벤토리(tier별 주소) 읽기. 담당 김준석(O2).

공개 함수: load_inventory. 결과는 RunContext.platform["onprem"] 모양이다(모양은
ddak.onprem.deploy provider docstring의 인벤토리 계약). 값·시크릿은 넣지 않는다.
이 모듈은 컨텍스트를 만들지 않는다(flow가 결과를 넣는다). AI import 금지(계약 1).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ddak.core.contracts.base import TIER_PATTERN
from ddak.core.contracts.errors import DdakToolError, ErrorCode

__all__ = ["load_inventory"]

_SECRET_KEY = re.compile(r"password|passwd|secret|token|key", re.IGNORECASE)
_TIER = re.compile(TIER_PATTERN)


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


# 필드는 onprem/deploy/provider.py docstring 계약과 같다. 값 상세 검증(포트·이름·env 허용 키)은
# O1 provider가 한다. 여기는 모양, 시크릿 키, 비로컬 docker_host만 막는다.
class _Volume(_M):
    name: str
    target: str
    read_only: bool = False


class _Tier(_M):
    name: str
    platform: Literal["linux/amd64", "linux/arm64"]
    ports: list[str] = Field(default_factory=list)
    network: str = "bridge"
    volumes: list[_Volume] = Field(default_factory=list)
    env_file: str | None = None
    public_env: dict[str, str] = Field(default_factory=dict)


class _Inventory(_M):
    docker_host: str | None = None
    tiers: dict[str, _Tier]

    @field_validator("docker_host")
    @classmethod
    def local_only(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("unix://"):
            raise ValueError("docker_host는 로컬 unix:// 소켓만 허용(ssh/tcp 미지원)")
        return value

    @field_validator("tiers")
    @classmethod
    def tier_names(cls, value: dict[str, _Tier]) -> dict[str, _Tier]:
        for name in value:
            if not _TIER.fullmatch(name):
                raise ValueError(f"tier 이름 형식 위반: {name!r}")
        return value


def _reject_secret_keys(node: object) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and _SECRET_KEY.search(key):
                raise DdakToolError(
                    ErrorCode.CONFIG_INVALID, f"인벤토리에 시크릿으로 보이는 키가 있다: {key}"
                )
            _reject_secret_keys(value)
    elif isinstance(node, list):
        for item in node:
            _reject_secret_keys(item)


def load_inventory(path: Path) -> dict[str, Any]:
    """platform.onprem.yaml을 읽어 {"docker_host": ..., "tiers": {...}}를 돌려준다."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, f"인벤토리를 읽을 수 없다: {path.name} ({type(exc).__name__})"
        ) from exc
    if not isinstance(raw, dict):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "인벤토리 최상위는 매핑이어야 한다")
    _reject_secret_keys(raw)
    try:
        return _Inventory.model_validate(raw).model_dump(mode="json")
    except ValidationError as exc:
        # 값은 시크릿일 수 있어 위치와 사유만 낸다.
        detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors()[:5])
        raise DdakToolError(ErrorCode.CONFIG_INVALID, f"인벤토리 검증 실패: {detail}") from exc
