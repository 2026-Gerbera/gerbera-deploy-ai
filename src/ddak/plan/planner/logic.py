"""generate_plan 본체: AI는 OPTIONAL step의 포함 여부만 제안한다(값·명령·경로 없음).

순서: Jev(ask_jev) -> Claude(call_ai) -> 규칙(빈 초안, fallback=True).
"""

from __future__ import annotations

from pathlib import Path

from ddak.core.ai.gateway import ask_jev, call_ai, get_jev_client
from ddak.core.ai.providers import LLMProvider, get_provider
from ddak.core.ai.providers.jev import JevAnswer, JevQuestion, JudgmentClient, client_identity
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import By, Layer, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Planner
from ddak.core.contracts.plan_draft import PlanDecisions, PlanDraft, StepDecision
from ddak.core.contracts.plan_facts import Facts
from ddak.core.contracts.step_catalog import StepDef, catalog_steps
from ddak.core.contracts.tools.generate_plan import GeneratePlanInput, GeneratePlanOutput

PROMPT_VERSION = "plan-v1"
_PROMPT = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")
_SOFT = (ErrorCode.AI_UNAVAILABLE, ErrorCode.AI_OUTPUT_INVALID)
_LLM_LABEL = {"api": "groq", "cli": "claude-cli", "replay": "replay"}


def decidable_steps(facts: Facts) -> list[StepDef]:
    """AI가 묻는 대상 = OPTIONAL step. 필수·조건부는 검증 코드가 정한다."""
    return [s for s in catalog_steps(facts.tiers, facts.target) if s.layer is Layer.OPTIONAL]


def summarize(facts: Facts) -> str:
    """키 이름·값·경로·해시 없이 개수와 tier 이름만."""
    changed = sorted({t for env in facts.changed.values() for t, v in env.items() if v})
    return (
        f"project {facts.project}; tiers {', '.join(facts.tiers)}; "
        f"changed tiers {', '.join(changed) or 'none'}; "
        f"smoke groups {', '.join(facts.smoke_groups) or 'base'}; "
        f"{len(facts.new_migrations)} new migrations; "
        f"{sum(k.is_new for k in facts.env_keys)} new env keys."
    )


def _rule(*, fallback: bool) -> PlanDraft:
    planner = Planner(by=By.RULE, provider="rule", attempts=0, fallback=fallback)
    return PlanDraft(decisions=(), planner=planner)


def _jev(
    steps: list[StepDef], facts: Facts, cfg: Settings, client: JudgmentClient | None
) -> PlanDraft:
    summary = summarize(facts)
    qs = [
        JevQuestion(
            id="step." + s.id.replace(".", "_"),
            kind="noul",
            text=f"Should the optional step {s.id} ({s.tool}) run for this deployment? {summary}",
        )
        for s in steps
    ]
    actual = client if client is not None else get_jev_client(cfg)
    answers = {a.id: a for a in ask_jev(state=summary, questions=qs, settings=cfg, client=actual)}
    provider_name, model = client_identity(actual)
    decisions: list[StepDecision] = []
    for s, q in zip(steps, qs, strict=True):
        a: JevAnswer | None = answers.get(q.id)
        if a is None or a.probability is None:
            continue
        decisions.append(
            StepDecision(
                id=s.id,
                include=a.probability >= 0.5,
                reason=f"{provider_name} 확률 {a.probability:.2f}",
            )
        )
    source = getattr(actual, "source", None)
    planner = Planner(
        by=By.AI,
        provider=provider_name,
        model=model,
        attempts=1,
        source=source if isinstance(source, Source) else Source.LIVE,
    )
    return PlanDraft(decisions=tuple(decisions), planner=planner)


def _claude(
    steps: list[StepDef],
    facts: Facts,
    feedback: tuple[str, ...],
    cfg: Settings,
    provider: LLMProvider | None,
) -> PlanDraft:
    allowed = {s.id for s in steps}
    data = f"facts: {summarize(facts)}\ncandidates: {', '.join(sorted(allowed))}"
    if feedback:
        data += "\nvalidation feedback:\n" + "\n".join(f"- {f}" for f in feedback)
    actual = provider if provider is not None else get_provider(cfg)
    res = call_ai(
        instruction=_PROMPT,
        data=data,
        output_model=PlanDecisions,
        prompt_version=PROMPT_VERSION,
        settings=cfg,
        provider=actual,
    )
    kept = tuple(d for d in res.value.decisions if d.id in allowed)
    planner = Planner(
        by=By.AI,
        provider=_LLM_LABEL.get(actual.name, actual.name),
        model=(
            res.usage.model
            if res.usage is not None and res.usage.model != "claude-cli-default"
            else cfg.llm_model
        ),
        source=res.source,
        attempts=res.attempts,
    )
    return PlanDraft(decisions=kept, planner=planner)


def generate_plan(
    inp: GeneratePlanInput,
    ctx: RunContext,
    *,
    jev_client: JudgmentClient | None = None,
    provider: LLMProvider | None = None,
    settings: Settings | None = None,
) -> GeneratePlanOutput:
    """호출은 tool_context("generate_plan", run_id) 안에서만 허용된다(flow가 건다)."""
    steps = decidable_steps(inp.facts)
    if not steps:
        return GeneratePlanOutput(draft=_rule(fallback=False), source=Source.LIVE)
    cfg = settings or Settings.from_env()
    draft: PlanDraft | None = None
    try:
        draft = _jev(steps, inp.facts, cfg, jev_client)
    except DdakToolError as exc:
        if exc.code not in _SOFT:
            raise
    if draft is None:
        try:
            draft = _claude(steps, inp.facts, inp.feedback, cfg, provider)
        except DdakToolError as exc:
            if exc.code not in _SOFT:
                raise
    return GeneratePlanOutput(
        draft=draft or _rule(fallback=True), source=(draft.planner.source if draft else Source.LIVE)
    )
