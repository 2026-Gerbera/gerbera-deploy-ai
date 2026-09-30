"""plan/validate: 계획 검증(결정적 검사기). 담당 김준석(O2).

공개 함수: validate_plan. 다른 디렉토리는 이 파일의 공개 함수만 쓴다.
AI를 import하지 않는다(import-linter 계약).
빈 구현이다. 구현이 끝나면 이 디렉토리에 tool.py를 만들고 @tool("<이름>")으로 등록한다
(시그니처: inp: <Tool>Input, ctx: RunContext -> <Tool>Output, 모델은 ddak.core.contracts.tools).

결정적 검사기다. ddak.core.ai, plan/planner, plan/dockerfile의 생성기를 import하지 않는다
(import-linter 계약 5).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.plan import Plan

_TODO = "plan/validate 미구현: 담당 김준석"


def validate_plan(inp: object, ctx: RunContext) -> Plan:
    """validate_plan 빈 구현.

    입력(모델 미정): PlanDraft + step 카탈로그 + facts. 출력: 최종 Plan(core/contracts/plan.py).
    step 층(내장/필수/조건부/선택)을 강제한다. 불합격이면 재지시 1회 -> 규칙 계획.
    """
    raise NotImplementedError(_TODO)
