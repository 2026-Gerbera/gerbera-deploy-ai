"""최종 결과 카드 툴 계약(C3). 판정과 심각도는 규칙만 정한다."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ddak.core.contracts.base import ContractModel, RunId, ToolInput
from ddak.core.contracts.release import Sha256


class ReportIssue(ContractModel):
    severity: Literal["info", "warning", "high", "critical"]
    code: str = Field(min_length=1, max_length=80)
    env: Literal["local", "cloud", "both"]
    title: str = Field(min_length=1, max_length=200)
    suggested_next: str = Field(default="", max_length=200)


class PostReportInput(ToolInput):
    pass


class PostReportOutput(ContractModel):
    run_id: RunId
    passed: bool = True
    status: Literal["ready"] = "ready"
    summary: str = Field(max_length=400)
    source: Literal["rule", "live", "cache", "replay"] = "rule"
    issues: list[ReportIssue] = Field(default_factory=list)
    image_index_digests: dict[str, Sha256] = Field(default_factory=dict)
    platform_digests: dict[str, dict[str, Sha256]] = Field(default_factory=dict)
