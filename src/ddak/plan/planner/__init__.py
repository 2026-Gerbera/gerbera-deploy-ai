"""plan/planner: 계획 생성(AI 초안). 담당 김준석(O2).

공개 함수: generate_plan. 다른 디렉토리는 이 파일의 공개 함수만 쓴다.
AI 호출은 ddak.core.ai(call_ai, ask_jev)로만 한다(허용 디렉토리, import-linter 계약 2).
빈 구현이다. 구현이 끝나면 이 디렉토리에 tool.py를 만들고 @tool("<이름>")으로 등록한다
(시그니처: inp: <Tool>Input, ctx: RunContext -> <Tool>Output, 모델은 ddak.core.contracts.tools).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext

_TODO = "plan/planner 미구현: 담당 김준석"


def generate_plan(inp: object, ctx: RunContext) -> object:
    """generate_plan 빈 구현.

    입력(모델 미정): 분석 결과 + 변경 tier + step 카탈로그. 출력: PlanDraft(AI 초안, 모델은
    plan이 소유하며 아직 없다). 최종 Plan(core/contracts/plan.py)은 plan/validate가 만든다.
    """
    raise NotImplementedError(_TODO)
