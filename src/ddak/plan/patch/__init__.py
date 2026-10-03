"""plan/patch: AI 코드 수정 P0(토글 ON 전용). 담당 장민영(O3).

공개 함수: patch_config, patch_db_access, patch_storage(툴 자리, 빈 구현),
propose_intents(제품용 위치 의도 생성), propose_config_patch(기존 제안 API),
render_intents(결정적 렌더러), prepare_patch(검사·원장 파이프라인).
다른 디렉토리는 이 파일의 공개 이름만 쓴다.
AI 호출은 ddak.core.ai(call_ai, ask_jev)로만 한다(허용 디렉토리, import-linter 계약 2).
툴 입출력 계약이 정해지면 이 디렉토리에 tool.py를 만들고 @tool("patch_config")으로 등록한다
(시그니처: inp: <Tool>Input, ctx: RunContext -> <Tool>Output, 모델은 ddak.core.contracts.tools).
제품은 patch_config 호출자 문맥에서 propose_intents를 prepare_patch에 주입한다.
propose_config_patch API는 호환용으로 유지한다.
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.plan.patch.generate import (
    PatchDraft,
    PatchEdit,
    PatchProposal,
    PreviousPatch,
    find_targets,
    propose_config_patch,
    propose_intents,
)
from ddak.plan.patch.intents import EditIntent, render_intents
from ddak.plan.patch.pipeline import PatchPreparation, prepare_patch

__all__ = [
    "EditIntent",
    "PatchDraft",
    "PatchEdit",
    "PatchPreparation",
    "PatchProposal",
    "PreviousPatch",
    "find_targets",
    "patch_config",
    "patch_db_access",
    "patch_storage",
    "prepare_patch",
    "propose_config_patch",
    "propose_intents",
    "render_intents",
]

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
