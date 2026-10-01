"""클라우드 TLS 검증 툴 계약(C3)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ddak.core.contracts.base import ContractModel, RunId, ToolInput
from ddak.core.contracts.enums import Target


class TlsCheck(ContractModel):
    id: Literal["V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9"]
    name: str = Field(min_length=1, max_length=80)
    status: Literal["pass", "fail", "inconclusive"]
    detail: str = Field(default="", max_length=400)


class VerifyTlsInput(ToolInput):
    target: Target


class VerifyTlsOutput(ContractModel):
    run_id: RunId
    target: Target
    passed: bool
    checks: list[TlsCheck]
    elapsed_s: float = Field(ge=0)
