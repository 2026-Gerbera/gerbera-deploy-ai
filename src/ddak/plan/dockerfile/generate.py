"""plan/dockerfile/generate.py: generate_dockerfile(AI 초안). 담당 장민영(O3).

AI 호출은 ddak.core.ai(call_ai)로만 한다(허용 디렉토리, import-linter 계약 2).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext

_TODO = "plan/dockerfile 미구현: 담당 장민영"


def generate_dockerfile(inp: object, ctx: RunContext) -> object:
    """generate_dockerfile 빈 구현.

    입력(모델 미정): 분석 결과(런타임·포트·의존성 파일). Dockerfile이 없을 때만 부른다.
    출력: Dockerfile 초안 텍스트 + 해시(비root, 버전 고정 베이스, 비밀값 COPY 금지).
    """
    raise NotImplementedError(_TODO)
