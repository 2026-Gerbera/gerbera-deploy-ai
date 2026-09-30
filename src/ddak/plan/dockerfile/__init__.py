"""plan/dockerfile: Dockerfile 생성·검사. 담당 장민영(O3).

공개 함수: generate_dockerfile(generate.py, AI 초안), validate_dockerfile(validate.py, 결정적 검사).
다른 디렉토리는 이 파일이 노출하는 공개 함수만 쓴다.
Dockerfile이 없을 때만 AI 초안 -> 정적 검사 + 검증 빌드 -> 사람 승인 -> 저장·재사용.
validate.py는 ddak.core.ai와 generate.py를 import하지 않는다(import-linter 계약 5).
빈 구현이다. 구현이 끝나면 tool.py를 만들고 @tool("<이름>")으로 등록한다.
"""

from __future__ import annotations

from ddak.plan.dockerfile.generate import generate_dockerfile
from ddak.plan.dockerfile.validate import validate_dockerfile

__all__ = ["generate_dockerfile", "validate_dockerfile"]
