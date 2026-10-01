"""LLM 응답 화이트리스트. 이 모양 밖의 어떤 것도 LLM은 낼 수 없다(extra=forbid)."""

from __future__ import annotations

from pydantic import Field

from ddak.core.contracts.base import ContractModel
from ddak.core.contracts.plan import STEP_ID_PATTERN, Planner

__all__ = ["PlanDecisions", "PlanDraft", "StepDecision"]


class StepDecision(ContractModel):
    id: str = Field(pattern=STEP_ID_PATTERN)
    include: bool
    reason: str = Field(max_length=200)


class PlanDecisions(ContractModel):
    """call_ai output_model(= LLM 응답 스키마)."""

    decisions: tuple[StepDecision, ...]


class PlanDraft(ContractModel):
    decisions: tuple[StepDecision, ...]
    planner: Planner
