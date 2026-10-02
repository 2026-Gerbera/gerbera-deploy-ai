"""생성기(O2) → 조립부(O1) 번들 계약. 자격증명·state·실행 명령은 받지 않는다."""

from typing import Literal

from pydantic import Field

from ddak.core.contracts.base import ContractModel, ToolInput
from ddak.core.contracts.enums import Source
from ddak.core.contracts.release import Sha256


class GenerateInfraInput(ToolInput):
    directory: str  # 조립부가 만든 run 전용 출력 디렉터리
    layer: Literal["platform", "app"]


class GenerateInfraOutput(ContractModel):
    directory: str
    layer: Literal["platform", "app"]
    files: dict[str, Sha256] = Field(min_length=1, max_length=64)
    outputs: dict[str, tuple[str, str]] = Field(default_factory=dict)
    source: Source = Source.LIVE
