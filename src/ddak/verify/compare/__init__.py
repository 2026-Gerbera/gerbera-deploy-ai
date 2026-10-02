"""verify/compare: 교차 검증(두 환경 결과 비교). 담당 장민영(O3).

공개 함수: compare_env_results. 다른 디렉토리는 이 파일의 공개 함수만 쓴다.
AI를 import하지 않는다(import-linter 계약). 등록은 tool.py(@tool("compare_env_results")).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.compare_env_results import (
    CompareEnvResultsInput,
    CompareEnvResultsOutput,
)


def compare_env_results(inp: CompareEnvResultsInput, ctx: RunContext) -> CompareEnvResultsOutput:
    """이번 run의 local·cloud smoke 결과와 이미지 관측을 비교한다."""
    from ddak.verify.compare.tool import compare_env_results as registered

    return registered(inp, ctx)


__all__ = ["compare_env_results"]
