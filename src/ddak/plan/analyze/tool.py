"""analyze_project 툴 등록(얇은 래퍼). AI는 tool_context("analyze_project") 안에서만 허용된다."""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.analyze_project import AnalyzeProjectInput, AnalyzeProjectOutput
from ddak.core.registry import tool
from ddak.plan.analyze import analyze_project as _analyze


@tool("analyze_project")
def analyze_project(inp: AnalyzeProjectInput, ctx: RunContext) -> AnalyzeProjectOutput:
    """tier·Dockerfile 유무·환경 키(plain/secret)를 분석한다. 규칙이 바닥, Jev는 애매한 키만."""
    return _analyze(inp, ctx)
