"""O1 공통 CD 입력·결과. 비밀값은 없고 경로/관측값만 전달한다."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ddak.core.contracts.base import ContractModel, LockToken, TierName, ToolInput
from ddak.core.contracts.enums import Target
from ddak.core.contracts.release import ImageObservation


class CdInput(ToolInput):
    target: Target
    lock_token: LockToken


class TierInput(CdInput):
    tier: TierName


class CdOutput(ContractModel):
    provider: str
    function: str
    applicable: bool = True
    changed: bool = False
    detail: str = ""
    passed: bool = True
    image_ref: str | None = None
    previous_image: str | None = None
    observation: ImageObservation | None = None
    migration: dict[str, Any] | None = None
    config_ref: str | None = None
    keys: list[str] = Field(default_factory=list)
