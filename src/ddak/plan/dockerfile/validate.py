"""plan/dockerfile/validate.py: validate_dockerfile(결정적 검사기). 담당 장민영(O3).

ddak.core.ai와 generate.py를 import하지 않는다(import-linter 계약 5).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext

_TODO = "plan/dockerfile 미구현: 담당 장민영"


def validate_dockerfile(inp: object, ctx: RunContext) -> object:
    """validate_dockerfile 빈 구현.

    입력(모델 미정): Dockerfile 텍스트(신뢰하지 않는 입력). 출력: 정적 검사(hadolint 등 +
    보안 기본값) 결과와 검증 빌드 성공 여부(push 없음).
    """
    raise NotImplementedError(_TODO)
