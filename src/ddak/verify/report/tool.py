"""post_report 레지스트리 연결."""

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.post_report import PostReportInput, PostReportOutput
from ddak.core.registry import tool
from ddak.verify.report import post_report as build_report


@tool("post_report")
def post_report(inp: PostReportInput, ctx: RunContext) -> PostReportOutput:
    return build_report(inp, ctx)
