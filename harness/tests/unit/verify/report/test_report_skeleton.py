"""C3 규칙 기반 보고는 AI나 외부 서비스 없이 항상 만들어진다."""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.post_report import PostReportInput
from ddak.verify.report import post_report


def test_rule_report_marks_missing_inputs_without_failing_report_step() -> None:
    result = post_report(PostReportInput(run_id="run-1"), RunContext("run-1"))
    assert result.passed is True
    assert result.source == "rule"
    assert {issue.code for issue in result.issues} == {
        "cloud_domain_missing",
        "image_artifacts_missing",
    }
