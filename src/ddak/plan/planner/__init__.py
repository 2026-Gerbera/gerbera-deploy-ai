"""plan/planner: 계획 생성(AI 초안). 담당 정준우(O1, O2 승계).

공개: generate_plan. AI 호출은 ddak.core.ai(ask_jev, call_ai)로만 한다(계약 2).
plan/validate를 import하지 않는다(공유는 core/contracts/step_catalog).
"""

from __future__ import annotations

from ddak.plan.planner.logic import generate_plan

__all__ = ["generate_plan"]
