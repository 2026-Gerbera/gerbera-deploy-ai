"""결정적 규칙 카드. 실행 성공·실패 판정은 실행기 결과를 그대로 사용한다."""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.post_report import PostReportInput, PostReportOutput
from ddak.verify.report.rules import issues_for


def post_report(inp: PostReportInput, ctx: RunContext) -> PostReportOutput:
    artifacts = ctx.release_artifacts
    indexes = (
        {tier: image.index_digest for tier, image in artifacts.images.items()} if artifacts else {}
    )
    platforms = (
        {tier: dict(image.platform_digests) for tier, image in artifacts.images.items()}
        if artifacts
        else {}
    )
    issues = issues_for(ctx)
    summary = "배포 실행 결과가 기록되었습니다"
    if issues:
        summary = f"배포 실행 결과가 기록되었습니다. 확인할 항목 {len(issues)}개"
    return PostReportOutput(
        run_id=inp.run_id,
        summary=summary,
        issues=issues,
        image_index_digests=indexes,
        platform_digests=platforms,
    )
