"""analyze 디렉토리 툴 등록(얇은 래퍼). AI는 tool_context("<툴 이름>") 안에서만 허용된다.

- analyze_project: 파이프라인 ① 플랜 분석.
- answer_code_question: 운영용 코드 질문(40개 밖, step 카탈로그에 없음).
  관리 웹이 조립부(ddak.app)를 거쳐 부른다.
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.analyze_project import AnalyzeProjectInput, AnalyzeProjectOutput
from ddak.core.contracts.tools.answer_code_question import (
    AnswerCodeQuestionInput,
    AnswerCodeQuestionOutput,
)
from ddak.core.registry import CODE_QUESTION, tool
from ddak.plan.analyze import analyze_project as _analyze
from ddak.plan.analyze import answer_code_question as _answer


@tool("analyze_project")
def analyze_project(inp: AnalyzeProjectInput, ctx: RunContext) -> AnalyzeProjectOutput:
    """tier·Dockerfile 유무·환경 키(plain/secret)를 분석한다. 규칙이 바닥, Jev는 애매한 키만."""
    return _analyze(inp, ctx)


@tool(CODE_QUESTION)
def answer_code_question(inp: AnswerCodeQuestionInput, ctx: RunContext) -> AnswerCodeQuestionOutput:
    """고른 소스 파일(커밋 고정)을 근거로 코드 질문에 답한다. 읽기 전용."""
    return _answer(inp, ctx)
