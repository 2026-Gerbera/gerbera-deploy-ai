"""generate_plan: AI가 step 포함 여부 초안(PlanDraft)을 제안한다."""

from __future__ import annotations

from ddak.core.contracts.base import AIUsage, ContractModel, ToolInput
from ddak.core.contracts.enums import Source
from ddak.core.contracts.plan_draft import PlanDraft
from ddak.core.contracts.plan_facts import Facts


class GeneratePlanInput(ToolInput):
    facts: Facts
    feedback: tuple[str, ...] = ()  # 재지시 사유


class GeneratePlanOutput(ContractModel):
    draft: PlanDraft
    source: Source | None = None
    ai_usage: AIUsage | None = None
