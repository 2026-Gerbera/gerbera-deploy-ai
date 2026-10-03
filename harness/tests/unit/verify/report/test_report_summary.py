"""source=fixture: 실제 모델 없이 결과 요약의 시간·입력·상태 경계를 확인한다."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from ddak.core.config import AdapterMode, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.post_report import (
    PostReportInput,
    PostReportOutput,
    ReportFacts,
    ReportNarrative,
)
from ddak.core.registry import spec_for
from ddak.executor.reporting import report_facts
from ddak.verify.report import logic
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_ui_integration_fix10 import prepare

NARRATIVE = ReportNarrative(
    conclusion="배포를 완료했습니다.",
    changes=["WAS 코드를 반영했습니다.", "새 이미지를 기록했습니다."],
    checks=["헬스 통과"],
    next_action="서비스를 확인하세요.",
)


def test_post_report_fake_ai_schema_and_redacted_facts(monkeypatch):
    secret = "fixture-" + "private-report"
    facts = report_facts(
        {
            "status": "SUCCEEDED",
            "result": {
                "steps": {
                    "verify.health.local": {
                        "status": "succeeded",
                        "elapsed_s": 2,
                        "error": secret,
                        "output": {"raw": secret},
                    }
                }
            },
        },
        {"images": {"was": "repo@sha256:" + "a" * 64}},
        {
            "patches": [{"before": secret}],
            "findings": [
                {"file": "app.py", "line": 2, "pattern_id": "secret_key", "value": secret}
            ],
        },
    )
    captured = []

    def call(**kw):
        captured.append(kw)
        return SimpleNamespace(value=NARRATIVE)

    monkeypatch.setattr(logic, "call_ai", call)
    monkeypatch.setattr(logic.Settings, "from_env", lambda: Settings())
    output = logic.post_report(
        PostReportInput(run_id="summary", facts=facts),
        RunContext("summary", adapter_mode=AdapterMode.REAL),
    )
    assert output.source == "ai" and output.narrative == NARRATIVE
    assert captured[0]["output_model"] is ReportNarrative
    assert secret not in captured[0]["data"]
    assert "before" not in captured[0]["data"] and "raw" not in captured[0]["data"]
    assert captured[0]["settings"].ai_timeout_s == 20 and captured[0]["settings"].ai_retries == 0


def test_post_report_error_returns_rule(monkeypatch):
    monkeypatch.setattr(logic.Settings, "from_env", lambda: Settings())

    def fail(**kw):
        raise TimeoutError

    monkeypatch.setattr(logic, "call_ai", fail)
    report = logic.post_report(
        PostReportInput(run_id="summary", facts=ReportFacts(status="FAILED_CLOUD")),
        RunContext("summary", adapter_mode=AdapterMode.REAL),
    )
    assert report.source == "rule" and report.passed is True
    assert report.narrative and "완료하지 못했습니다" in report.narrative.conclusion


@pytest.mark.anyio
@pytest.mark.parametrize("timeout", [False, True])
async def test_summary_after_seal_does_not_delay_or_change_state(rig, monkeypatch, timeout):
    from ddak.executor import reporting

    service, source, _ = rig
    barrier = asyncio.Event()
    monkeypatch.setattr(
        service.registry,
        "_specs",
        {**service.registry._specs, "post_report": spec_for("post_report")},
    )

    @service.registry.tool("post_report")
    async def report(inp: PostReportInput, ctx: RunContext) -> PostReportOutput:
        assert service.get_run(inp.run_id)["status"] == "SUCCEEDED"
        assert service.get_release(inp.run_id) is not None
        await barrier.wait()
        return PostReportOutput(
            run_id=inp.run_id, summary=NARRATIVE.conclusion, narrative=NARRATIVE, source="ai"
        )

    monkeypatch.setattr(reporting, "SUMMARY_TIMEOUT", 0.03 if timeout else 2)
    rid = prepare(service, source)
    service.approve(rid, approver="fixture", approved=True)
    service.start(rid)
    await asyncio.wait_for(service.wait(rid), timeout=2)
    before = json.dumps(service.store.run(rid), sort_keys=True)
    release = json.dumps(service.get_release(rid), sort_keys=True)
    assert service.reports.get(rid)["state"] == "pending"
    if not timeout:
        barrier.set()
    await service.reports.tasks[rid]
    result = service.reports.get(rid)
    assert result["state"] == "ready" and result["source"] == ("rule" if timeout else "ai")
    assert before == json.dumps(service.store.run(rid), sort_keys=True)
    assert release == json.dumps(service.get_release(rid), sort_keys=True)
