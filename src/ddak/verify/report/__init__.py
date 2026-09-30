"""verify/report: 결과 카드·보고. 담당 양서윤(C3).

공개 함수: post_report. 다른 디렉토리는 이 파일의 공개 함수만 쓴다.
AI 호출은 ddak.core.ai(call_ai, ask_jev)로만 한다(허용 디렉토리, import-linter 계약 2).
빈 구현이다. 구현이 끝나면 이 디렉토리에 tool.py를 만들고 @tool("<이름>")으로 등록한다
(시그니처: inp: <Tool>Input, ctx: RunContext -> <Tool>Output, 모델은 ddak.core.contracts.tools).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext

_TODO = "verify/report 미구현: 담당 양서윤"


def post_report(inp: object, ctx: RunContext) -> object:
    """post_report 빈 구현.

    입력(모델 미정): run 결과(이벤트, 검증 결과, 교차 검증, 원인 설명). 출력: 결과 카드 데이터
    (관리 웹 표시용) + 선택적 AI 요약(source 라벨).
    Slack 알림은 ddak.integrations.slack 공개 함수로 보낸다.
    """
    raise NotImplementedError(_TODO)
