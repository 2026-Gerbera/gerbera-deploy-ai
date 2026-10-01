"""AI 없이 적용하는 보고 규칙."""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.post_report import ReportIssue


def issues_for(ctx: RunContext) -> list[ReportIssue]:
    issues: list[ReportIssue] = []
    if not ctx.cloud_domain:
        issues.append(
            ReportIssue(
                severity="high",
                code="cloud_domain_missing",
                env="cloud",
                title="클라우드 도메인이 설정되지 않았습니다",
                suggested_next="관리 페이지에서 도메인을 저장하고 다시 실행하세요",
            )
        )
    if ctx.release_artifacts is None:
        issues.append(
            ReportIssue(
                severity="warning",
                code="image_artifacts_missing",
                env="both",
                title="검증할 이미지 산출물이 없습니다",
                suggested_next="빌드 단계의 digest 기록을 확인하세요",
            )
        )
    return issues
