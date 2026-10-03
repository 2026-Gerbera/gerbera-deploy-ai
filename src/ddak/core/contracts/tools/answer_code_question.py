"""answer_code_question: 앱 저장소 소스(git 커밋 고정)를 근거로 운영자 코드 질문에 답한다.

운영용 읽기 전용 AI 툴이다. 파이프라인 step이 아니며(step 카탈로그에 없음) 배포·패치·커밋을
하지 않는다. 파일 선택·비밀 경로 제외·크기 상한은 코드(ddak.core.code_question)가 정하고,
AI는 고른 파일만 데이터로 받는다.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from ddak.core.contracts.base import AIUsage, ContractModel, ToolInput
from ddak.core.contracts.enums import Source

# AI에 넣는 코드 문맥 총량 상한(UTF-8 바이트). 파일 내용과 파일 목록을 합친 값이다.
MAX_CONTEXT_BYTES = 60_000
MAX_QUESTION_CHARS = 2000
SHA_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"


class CodeFile(ContractModel):
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=MAX_CONTEXT_BYTES)
    truncated: bool = False  # 크기 상한 때문에 앞부분만 담았다


class AnswerCodeQuestionInput(ToolInput):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    commit: str = Field(pattern=SHA_PATTERN)  # 기준 커밋(감시 브랜치 최신)
    branch: str = Field(min_length=1, max_length=255)
    files: tuple[CodeFile, ...] = Field(default=(), max_length=80)
    paths: tuple[str, ...] = Field(default=(), max_length=3000)  # 비밀 경로를 뺀 파일 목록

    @model_validator(mode="after")
    def _bounded(self) -> AnswerCodeQuestionInput:
        size = sum(len(f.content.encode()) for f in self.files)
        size += sum(len(p.encode()) + 1 for p in self.paths)
        if size > MAX_CONTEXT_BYTES:
            raise ValueError(f"코드 문맥이 {MAX_CONTEXT_BYTES}바이트 상한을 넘는다")
        return self


class AnswerCodeQuestionOutput(ContractModel):
    answer: str
    sources: tuple[str, ...] = ()  # 근거 파일 경로(입력 files 안 경로만)
    commit: str = Field(pattern=SHA_PATTERN)
    source: Source | None = None
    ai_usage: AIUsage | None = None
