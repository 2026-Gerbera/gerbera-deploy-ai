"""diagnose_parity_gap 툴: 실행기 입력(reason·tracks·failed_steps) → 규칙 진단. 설명 전용."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from ddak.app import load_tools
from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.config import LLMBackend, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source, Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.compare_env_results import CompareCheck, CompareEnvResultsOutput
from ddak.core.contracts.tools.diagnose_parity_gap import (
    DiagnoseParityGapInput,
    DiagnoseParityGapOutput,
)
from ddak.core.contracts.tools.smoke_test import SmokeScenario, SmokeTestOutput
from ddak.core.runtime import tool_context
from ddak.verify.diagnose import diagnose_parity_gap
from ddak.verify.smoke import results

RUN = "run-diag-tool-1"
CFG = Settings(ai_retries=0, llm_backend=LLMBackend.API, llm_model="m-claude")
CTX = RunContext(RUN)
# 결과에 나오면 안 되는 값. 비밀값 스캐너에 걸리지 않게 이어 붙여 만든다(AGENTS 4절)
FAKE_SECRET = "not-a-real-" + "password-456"


class FakeProvider:
    name = "fake"

    def __init__(self, *replies: str, fail: bool = False) -> None:
        self.replies = list(replies)
        self.fail = fail
        self.seen: list[AIRequest] = []

    def complete(self, req: AIRequest) -> AIResponse:
        self.seen.append(req)
        if self.fail:
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "연결 실패")
        if not self.replies:
            raise AssertionError("AI를 부르면 안 된다")
        return AIResponse(text=self.replies.pop(0), source=Source.REPLAY)


@pytest.fixture(autouse=True)
def _clean_store() -> Iterator[None]:
    results.clear()
    yield
    results.clear()


def step(tool: str, status: str = "failed", **kw: Any) -> dict[str, Any]:
    return {"step_id": f"x.{tool}", "tool": tool, "status": status, **kw}


def run(
    reason: str, steps: list[dict[str, Any]], provider: FakeProvider | None = None
) -> DiagnoseParityGapOutput:
    # 실행기 #18 _diagnose가 넘기는 모양 그대로 검증한다(extra=forbid)
    inp = DiagnoseParityGapInput.model_validate(
        {
            "run_id": RUN,
            "reason": reason,
            "tracks": {"build": "DONE", "local": "ROLLED_BACK", "cloud": "DONE"},
            "failed_steps": steps,
        }
    )
    with tool_context("diagnose_parity_gap", RUN):
        return diagnose_parity_gap(inp, CTX, provider=provider or FakeProvider(), settings=CFG)


def test_track_failure_message_sets_category_without_leaking_text() -> None:
    error = f"MissingEnvError: 필수 환경변수 SECRET_KEY가 없습니다 ({FAKE_SECRET})"
    out = run("track_failed", [step("deploy_tier", target="local", error=error)])
    assert out.category == "secret_missing" and out.rule == "missing_env"
    assert out.is_hypothesis is False and out.reason == "track_failed"
    assert FAKE_SECRET not in out.model_dump_json()


def test_parity_failure_reads_compare_output_from_failed_step() -> None:
    compare = CompareEnvResultsOutput(
        run_id=RUN,
        passed=False,
        unexpected_diffs=1,
        elapsed_s=0.0,
        checks=[CompareCheck(id="S0.version.db", verdict="mismatch", local="a", cloud="b")],
    )
    steps = [step("compare_env_results", "check_failed", output=compare.model_dump(mode="json"))]
    out = run("parity_failed", steps)
    assert out.category == "parity_diff"
    assert [(e.source, e.ref) for e in out.evidence] == [("diff", "S0.version.db")]


def test_smoke_output_from_failed_step_and_store_are_used() -> None:
    ready = SmokeScenario(
        id="S0.ready",
        ok=False,
        status=503,
        normalized={"status": "fail", "schema.current": "0001", "schema.expected": "0002"},
    )
    smoke = SmokeTestOutput(
        run_id=RUN, target=Target.LOCAL, passed=False, elapsed_s=0.1, scenarios=[ready]
    )
    failed = step("smoke_test", target="local", output=smoke.model_dump(mode="json"))
    out = run("track_failed", [failed])
    assert out.category == "db_schema"
    results.record(smoke)  # 보관소에만 있어도 같은 결과
    assert run("track_failed", []).category == "db_schema"


def test_unknown_is_a_hypothesis_and_bad_output_is_ignored() -> None:
    steps = [step("compare_env_results", "check_failed", output={"not": "compare"})]
    out = run("parity_failed", steps)
    assert out.category == "unknown" and out.is_hypothesis is True and out.rule is None
    assert out.source is None and out.ai_usage is None


def test_executor_input_shape_is_strict() -> None:
    with pytest.raises(ValueError):
        DiagnoseParityGapInput.model_validate({"run_id": RUN, "reason": "other"})
    with pytest.raises(ValueError):
        DiagnoseParityGapInput.model_validate(
            {"run_id": RUN, "reason": "track_failed", "failed_steps": [step("x", "succeeded")]}
        )


def test_diagnose_is_registered() -> None:
    assert "diagnose_parity_gap" in load_tools().registered()


def test_rule_hit_does_not_call_ai() -> None:
    provider = FakeProvider()
    run("track_failed", [step("deploy_tier", error="KeyError: 'SECRET_KEY'")], provider)
    assert provider.seen == []


def test_unknown_gets_ai_explanation_as_hypothesis_with_checked_evidence() -> None:
    error = f"weird failure token={FAKE_SECRET} at boot"
    answer = {
        "category": "config_mismatch",
        "summary": "기동 설정이 환경과 맞지 않았을 가능성이 크다",
        "suggested_next": "was 컨테이너 기동 로그를 확인한다",
        "evidence": ["log0", "log9", "made-up"],  # 없는 ref는 버린다
    }
    provider = FakeProvider(json.dumps(answer))
    out = run("track_failed", [step("deploy_tier", target="local", error=error)], provider)
    assert out.category == "config_mismatch" and out.is_hypothesis is True and out.rule is None
    assert [(e.source, e.ref) for e in out.evidence] == [("log", "log#1")]
    assert out.source is Source.REPLAY
    assert FAKE_SECRET not in provider.seen[0].user  # call_ai가 가린다
    assert "[log0]" in provider.seen[0].user


def test_ai_unavailable_falls_back_to_rule_unknown() -> None:
    out = run("track_failed", [step("deploy_tier", error="weird")], FakeProvider(fail=True))
    assert out.category == "unknown" and out.source is None and out.ai_usage is None


def test_ai_outside_the_tool_context_is_not_hidden() -> None:
    inp = DiagnoseParityGapInput(run_id=RUN, reason="track_failed")
    with pytest.raises(DdakToolError) as caught:
        diagnose_parity_gap(inp, CTX, provider=FakeProvider(), settings=CFG)
    assert caught.value.code is ErrorCode.AI_NOT_ALLOWED
