"""선택 가능한 파일 단위 설정 제안과 등록 patch_config의 검토 요청."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ddak.core.contracts.enums import Source

ProposalId = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}$")]
EnvName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")]


class ReviewEdit(BaseModel):
    """PatchEdit와 동일한 줄 범위·길이 제한. 번호는 변경 전 원본 기준이다."""

    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=200)
    start: int = Field(ge=1, le=100_000)
    end: int = Field(ge=0, le=100_000)
    lines: list[str] = Field(max_length=40)

    @field_validator("lines")
    @classmethod
    def single_lines(cls, value: list[str]) -> list[str]:
        if any("\n" in line or "\r" in line or len(line) > 400 for line in value):
            raise ValueError("lines의 각 항목은 한 줄(400자 이하)이어야 한다")
        return value


class CodeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: ProposalId
    title: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=300)
    edits: list[ReviewEdit] = Field(min_length=1, max_length=20)
    requires: list[ProposalId] = Field(default_factory=list, max_length=10)
    env_vars: list[EnvName] = Field(default_factory=list, max_length=10)
    revision: int = Field(default=1, ge=1)
    required: bool = False


class ProposalBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    proposals: list[CodeProposal] = Field(max_length=10)


@dataclass(frozen=True)
class ReviewResult:
    """검사한 제안과 관문이 보고한 출처. 승인·실행 권한을 뜻하지 않는다."""

    proposals: list[CodeProposal]
    source: Source


class PatchReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["propose", "revise", "compose"]
    proposals: list[CodeProposal] = Field(default_factory=list, max_length=10)
    selected: list[ProposalId] = Field(default_factory=list, max_length=10)
    proposal_id: str = ""
    prompt: str = Field(default="", max_length=2000)
