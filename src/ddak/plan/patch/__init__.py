"""plan/patch: AI 코드 수정 P0(토글 ON 전용). 담당 장민영(O3).

공개 함수: patch_config, patch_db_access, patch_storage. 다른 디렉토리는 이 파일의 공개 함수만 쓴다.
AI 호출은 ddak.core.ai(call_ai, ask_jev)로만 한다(허용 디렉토리, import-linter 계약 2).
빈 구현이다. 구현이 끝나면 이 디렉토리에 tool.py를 만들고 @tool("<이름>")으로 등록한다
(시그니처: inp: <Tool>Input, ctx: RunContext -> <Tool>Output, 모델은 ddak.core.contracts.tools).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext

_TODO = "plan/patch 미구현: 담당 장민영"


def patch_config(inp: object, ctx: RunContext) -> object:
    """patch_config 빈 구현.

    입력(모델 미정): 분석 결과 + 대상 파일(신뢰하지 않는 입력). 출력: 빌드 사본에 적용할 diff
    제안 + 원본·diff·수정본 해시. ctx.toggles["code_patch"]가 True일 때만, 사람 승인 뒤 적용한다.
    """
    raise NotImplementedError(_TODO)


def patch_db_access(inp: object, ctx: RunContext) -> object:
    """patch_db_access 빈 구현. 입출력은 patch_config와 같은 모양(DB 접근 패턴)."""
    raise NotImplementedError(_TODO)


def patch_storage(inp: object, ctx: RunContext) -> object:
    """patch_storage 빈 구현. 입출력은 patch_config와 같은 모양(파일 저장소 패턴)."""
    raise NotImplementedError(_TODO)
