"""diagnose_parity_gap 툴 진입점(설명 전용). 담당 장민영(O3).

실행기가 채운 입력(failed_steps의 redact된 실패 메시지·툴 출력)과 smoke 보관소를 모아
규칙(rules.py)으로 원인 범주를 먼저 정한다. 판정·롤백에 쓰이지 않는다(2026-10-03 diagnose-advisory).
- 실패 메시지는 로그로, compare 출력은 비교 결과로, smoke 출력은 smoke 신호로 쓴다.
- 출력 모양이 맞지 않는 기록은 버린다(진단이 실패해도 run에는 영향이 없지만 굳이 실패하지 않는다).
- 규칙이 못 잡으면(unknown)만 call_ai로 설명을 받는다. AI 결과는 항상 가설(is_hypothesis=True)이고,
  근거는 코드가 넘긴 위치 목록 안의 것만 남긴다. AI를 못 쓰면 규칙 결과(unknown)를 그대로 낸다.
- AI 입력: 실패 step(id·tool·대상·상태), 실패 메시지 끝부분, smoke 실패 시나리오, 비교 불일치 id.
  call_ai가 다시 redact하고 untrusted_data로 감싼다. 출력 원문(본문·헤더)은 넣지 않는다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ddak.core.ai.gateway import call_ai
from ddak.core.ai.providers import LLMProvider
from ddak.core.answer_language import with_answer_language
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.compare_env_results import CompareEnvResultsOutput
from ddak.core.contracts.tools.diagnose_parity_gap import (
    Category,
    DiagnoseEvidence,
    DiagnoseParityGapInput,
    DiagnoseParityGapOutput,
)
from ddak.core.contracts.tools.smoke_test import SmokeTestOutput
from ddak.verify.diagnose.rules import RuleDiagnosis, diagnose_by_rules
from ddak.verify.smoke import results_for

COMPARE_TOOL = "compare_env_results"
SMOKE_TOOL = "smoke_test"
PROMPT_VERSION = "diagnose-v1"
MAX_ERROR_CHARS = 1500  # AI에 넘기는 실패 메시지 하나의 끝부분
MAX_ITEMS = 20

INSTRUCTION = """\
너는 배포 파이프라인의 실패 원인 설명 작성기다.
판정은 이미 끝났고, 너의 답은 사람이 읽는 설명일 뿐이다.
아래 데이터는 한 run에서 실패한 단계, 실패 메시지 끝부분,
smoke 실패 시나리오, 두 환경 비교 불일치다.
알려진 규칙은 원인을 찾지 못했다. 데이터에 근거해 가장 그럴듯한 원인 범주를 하나 고르고 설명한다.
- category: secret_missing, db_schema, db_conn, db_tls, image_pull, health_timeout,
  config_mismatch, tls, parity_diff, unknown 중 하나. 근거가 약하면 unknown.
- summary: 한국어 300자 이내. 무엇이 왜 실패했을 가능성이 큰지. 비밀값·주소·계정 이름을 쓰지 않는다.
- suggested_next: 한국어 150자 이내. 사람이 다음에 확인할 것 하나.
- evidence: 근거로 쓴 항목의 ref만(데이터의 [ref] 표시 그대로, 최대 5개). 없는 ref를 만들지 않는다.
"""


class _AIDiagnosis(BaseModel):
    """call_ai 출력 모델(제안)."""

    model_config = ConfigDict(extra="forbid")

    category: Category
    summary: str = Field(min_length=1, max_length=400)
    suggested_next: str = Field(default="", max_length=200)
    evidence: list[str] = Field(default_factory=list, max_length=5)


Ref = tuple[Literal["log", "smoke", "diff"], str]


def _gather(
    inp: DiagnoseParityGapInput,
) -> tuple[list[str], dict[Target, SmokeTestOutput], CompareEnvResultsOutput | None]:
    logs = [s.error for s in inp.failed_steps if s.error]
    compare: CompareEnvResultsOutput | None = None
    smoke: dict[Target, SmokeTestOutput] = dict(results_for(inp.run_id))
    for step in inp.failed_steps:
        if step.output is None:
            continue
        try:
            if step.tool == COMPARE_TOOL and compare is None:
                compare = CompareEnvResultsOutput.model_validate(step.output)
            elif step.tool == SMOKE_TOOL:
                out = SmokeTestOutput.model_validate(step.output)
                smoke[out.target] = out  # 실패 기록의 출력이 보관소보다 이 run에 정확하다
        except ValidationError:
            continue
    return logs, smoke, compare


def _ai_data(
    inp: DiagnoseParityGapInput,
    logs: list[str],
    smoke: dict[Target, SmokeTestOutput],
    compare: CompareEnvResultsOutput | None,
) -> tuple[str, dict[str, Ref]]:
    """AI에 넘길 데이터와, 근거로 인정할 ref → (source, 결과에 남길 ref)."""
    refs: dict[str, Ref] = {}
    tracks = ", ".join(f"{k}={v}" for k, v in inp.tracks.items())
    lines = [f"reason: {inp.reason}", f"tracks: {tracks}"]
    lines.append("## 실패 단계")
    for s in inp.failed_steps[:MAX_ITEMS]:
        lines.append(f"- {s.step_id} tool={s.tool} target={s.target or '-'} status={s.status}")
    lines.append("## 실패 메시지(끝부분)")
    for i, text in enumerate(logs[:MAX_ITEMS]):
        ref = f"log{i}"
        refs[ref] = ("log", f"log{i}#1" if len(logs) > 1 else "log#1")
        lines.append(f"[{ref}] {text[-MAX_ERROR_CHARS:]}")
    lines.append("## smoke 실패 시나리오")
    for target, out in smoke.items():
        for sc in out.scenarios:
            if not sc.ok:
                ref = f"smoke:{target.value}:{sc.id}"
                refs[ref] = ("smoke", f"{target.value}:{sc.id}")
                lines.append(f"[{ref}] status={sc.status} detail={sc.detail}")
    if compare is not None:
        lines.append("## 비교 불일치")
        for check in compare.checks:
            if check.verdict == "mismatch":
                ref = f"diff:{check.id}"
                refs[ref] = ("diff", check.id)
                lines.append(f"[{ref}]")
    return "\n".join(lines), refs


def _from_rules(inp: DiagnoseParityGapInput, found: RuleDiagnosis) -> DiagnoseParityGapOutput:
    data = found.to_dict()
    return DiagnoseParityGapOutput(
        run_id=inp.run_id,
        reason=inp.reason,
        category=found.category,
        summary=str(data["summary"]),
        suggested_next=str(data["suggested_next"]),
        evidence=[DiagnoseEvidence(source=e.source, ref=e.ref[:64]) for e in found.evidence[:5]],
        is_hypothesis=found.is_hypothesis,
        rule=found.rule,
        matched_rules=found.matched_rules[:20],
    )


def diagnose_parity_gap(
    inp: DiagnoseParityGapInput,
    ctx: RunContext,
    *,
    provider: LLMProvider | None = None,
    settings: Settings | None = None,
) -> DiagnoseParityGapOutput:
    """툴 진입점. 실행기가 tool_context("diagnose_parity_gap") 안에서 부른다."""
    logs, smoke, compare = _gather(inp)
    found = diagnose_by_rules(logs=logs, smoke=smoke.values(), compare=compare)
    out = _from_rules(inp, found)
    if found.rule is not None:
        return out
    data, refs = _ai_data(inp, logs, smoke, compare)
    try:
        result = call_ai(
            instruction=with_answer_language(
                INSTRUCTION, ctx.project_settings.get("ai_answer_language", "ko")
            ),
            data=data,
            output_model=_AIDiagnosis,
            prompt_version=PROMPT_VERSION,
            settings=settings,
            provider=provider,
        )
    except DdakToolError as exc:
        if exc.code is ErrorCode.AI_NOT_ALLOWED:  # 경계 위반은 감추지 않는다
            raise
        return out  # AI를 못 쓰면(연결·형식 오류) 규칙 결과(unknown)
    ai = result.value
    evidence = [
        DiagnoseEvidence(source=refs[r][0], ref=refs[r][1][:64]) for r in ai.evidence if r in refs
    ]
    return DiagnoseParityGapOutput.model_validate(
        {
            **out.model_dump(),
            "category": ai.category,
            "summary": ai.summary,
            "suggested_next": ai.suggested_next,
            "evidence": evidence[:5],
            "is_hypothesis": True,
            "source": result.source,
            "ai_usage": result.usage,
        }
    )
