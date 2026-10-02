"""compare_env_results 입출력(골든패스 8-5 초안, 담당 O3).

두 환경(local·cloud)의 smoke_test 결과와 이미지 관측을 비교한다.
- 입력은 run_id뿐이다. 비교할 값은 이번 run의 smoke 결과와 RunContext.release_artifacts에서 읽는다.
- 판정: match / mismatch / expected_diff / skipped. mismatch가 있거나 match가 없으면 passed=False.
- 예상된 차이(expected_diff)는 값을 싣지 않는다(주소 등 노출 방지).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ddak.core.contracts.base import ContractModel, RunId, ToolInput
from ddak.core.contracts.tools.smoke_test import Observed


class CompareEnvResultsInput(ToolInput):
    """target 없음: 두 환경을 함께 본다."""


class CompareCheck(ContractModel):
    id: str = Field(min_length=1, max_length=96)
    verdict: Literal["match", "mismatch", "expected_diff", "skipped"]
    local: Observed = None
    cloud: Observed = None
    reason: str = Field(default="", max_length=200)


class CompareEnvResultsOutput(ContractModel):
    run_id: RunId
    passed: bool
    unexpected_diffs: int = Field(ge=0)
    checks: list[CompareCheck]
    elapsed_s: float = Field(ge=0)
