"""validate_plan: 초안(없으면 규칙 계획)을 검증·조립해 최종 Plan을 만든다(코드, AI 금지)."""

from __future__ import annotations

from ddak.core.contracts.base import ToolInput
from ddak.core.contracts.plan import Plan
from ddak.core.contracts.plan_draft import PlanDraft
from ddak.core.contracts.plan_facts import Facts

ValidatePlanOutput = Plan


class ValidatePlanInput(ToolInput):
    facts: Facts
    draft: PlanDraft | None = None  # None = 규칙 계획
