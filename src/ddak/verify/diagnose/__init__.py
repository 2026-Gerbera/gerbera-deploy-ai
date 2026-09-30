"""verify/diagnose: 원인 분석(AI). 담당 장민영(O3).

공개 함수: diagnose_parity_gap. 다른 디렉토리는 이 파일의 공개 함수만 쓴다.
AI 호출은 ddak.core.ai(call_ai, ask_jev)로만 한다(허용 디렉토리, import-linter 계약 2).
빈 구현이다. 구현이 끝나면 이 디렉토리에 tool.py를 만들고 @tool("<이름>")으로 등록한다
(시그니처: inp: <Tool>Input, ctx: RunContext -> <Tool>Output, 모델은 ddak.core.contracts.tools).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext

_TODO = "verify/diagnose 미구현: 담당 장민영"


def diagnose_parity_gap(inp: object, ctx: RunContext) -> object:
    """diagnose_parity_gap 빈 구현.

    입력(모델 미정): compare_env_results 차이 + redact된 진단 정보. 출력: 원인 설명(AI 제안,
    source 라벨 포함). 로그·diff는 신뢰하지 않는 입력이다.
    """
    raise NotImplementedError(_TODO)
