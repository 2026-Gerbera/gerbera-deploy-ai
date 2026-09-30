"""verify/smoke: 스모크 테스트. 담당 장민영(O3).

공개 함수: smoke_test. 다른 디렉토리는 이 파일의 공개 함수만 쓴다.
AI를 import하지 않는다(import-linter 계약).
빈 구현이다. 구현이 끝나면 이 디렉토리에 tool.py를 만들고 @tool("<이름>")으로 등록한다
(시그니처: inp: <Tool>Input, ctx: RunContext -> <Tool>Output, 모델은 ddak.core.contracts.tools).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext

_TODO = "verify/smoke 미구현: 담당 장민영"


def smoke_test(inp: object, ctx: RunContext) -> object:
    """smoke_test 빈 구현.

    입력(모델 미정): 대상(local|cloud)과 앱 주소(RunContext.platform/cloud_domain에서 읽음).
    출력: 시나리오별 통과·실패와 관찰값(교차 검증 입력). 비밀값·절대 경로를 결과에 넣지 않는다.
    """
    raise NotImplementedError(_TODO)
