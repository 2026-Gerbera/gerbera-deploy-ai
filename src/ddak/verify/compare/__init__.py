"""verify/compare: 교차 검증(두 환경 결과 비교). 담당 장민영(O3).

공개 함수: compare_env_results. 다른 디렉토리는 이 파일의 공개 함수만 쓴다.
AI를 import하지 않는다(import-linter 계약).
빈 구현이다. 구현이 끝나면 이 디렉토리에 tool.py를 만들고 @tool("<이름>")으로 등록한다
(시그니처: inp: <Tool>Input, ctx: RunContext -> <Tool>Output, 모델은 ddak.core.contracts.tools).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext

_TODO = "verify/compare 미구현: 담당 장민영"


def compare_env_results(inp: object, ctx: RunContext) -> object:
    """compare_env_results 빈 구현.

    입력(모델 미정): local·cloud의 smoke_test·health_check 결과. 출력: 환경 간 차이 목록과
    통과 여부(차이가 있으면 diagnose로 넘긴다).
    """
    raise NotImplementedError(_TODO)
