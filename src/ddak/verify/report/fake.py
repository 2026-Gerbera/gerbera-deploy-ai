"""UI fixture에서 사용할 규칙 보고. 실 구현과 같은 모델을 쓴다."""

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.post_report import PostReportInput, PostReportOutput
from ddak.verify.report.logic import post_report


def fake_post_report(inp: PostReportInput, ctx: RunContext) -> PostReportOutput:
    return post_report(inp, ctx)
