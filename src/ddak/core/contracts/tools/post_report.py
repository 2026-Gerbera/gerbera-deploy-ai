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


class ReportStep(ContractModel):
    id: str
    status: str
    elapsed_s: float | None = None
    error_code: str | None = None


class ReportFile(ContractModel):
    file: str
    tier: str
    change: str


class ReportFinding(ContractModel):
    file: str
    line: int | None = None
    kind: str


class ReportImage(ContractModel):
    tier: str
    digest_prefix: str = Field(pattern=r"^[a-f0-9]{12}$")


class ReportFacts(ContractModel):
    status: str
    steps: list[ReportStep] = Field(default_factory=list)
    files: list[ReportFile] = Field(default_factory=list)
    findings: list[ReportFinding] = Field(default_factory=list)
    images: list[ReportImage] = Field(default_factory=list)
    checks: list[ReportStep] = Field(default_factory=list)


class ReportNarrative(ContractModel):
    conclusion: str = Field(min_length=1, max_length=200)
    changes: list[str] = Field(min_length=2, max_length=3)
    checks: list[str] = Field(max_length=8)
    next_action: str = Field(min_length=1, max_length=400)


class PostReportInput(ToolInput):
    facts: ReportFacts | None = None


class PostReportOutput(ContractModel):
    run_id: RunId
    passed: bool = True
    status: Literal["ready"] = "ready"
    summary: str = Field(max_length=400)
    source: Literal["rule", "live", "cache", "replay", "ai"] = "rule"
    issues: list[ReportIssue] = Field(default_factory=list)
    image_index_digests: dict[str, Sha256] = Field(default_factory=dict)
    platform_digests: dict[str, dict[str, Sha256]] = Field(default_factory=dict)

    narrative: ReportNarrative | None = None
