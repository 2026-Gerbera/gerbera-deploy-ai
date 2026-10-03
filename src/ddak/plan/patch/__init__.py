"""설정 패치 공개 API. 제품은 tool.py에 한 번 등록된 patch_config를 호출한다.

등록 툴은 intents 생성과 검사·성공 원장 재사용을 함께 수행한다.
patch_session은 조립부의 실행별 설정·경로만 주입하며 툴을 직접 호출하지 않는다.
토글 ON은 새 AI 제안, OFF는 AI 없이 이전 승인 패치 유지만 허용한다. 손실이면 승인 전에 멈춘다.
propose_config_patch는 파일별 재적용·patch_lost 관문을 가진 이전 줄 편집 API 호환용이다.
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.plan.patch.generate import (
    PatchDraft,
    PatchEdit,
    PatchProposal,
    PreviousPatch,
    find_targets,
    patch_config,
    propose_config_patch,
    propose_intents,
)
from ddak.plan.patch.intents import EditIntent, render_intents
from ddak.plan.patch.pipeline import PatchPreparation, PatchSession, patch_session, prepare_patch

__all__ = [
    "EditIntent",
    "PatchDraft",
    "PatchEdit",
    "PatchPreparation",
    "PatchProposal",
    "PatchSession",
    "PreviousPatch",
    "find_targets",
    "patch_config",
    "patch_db_access",
    "patch_session",
    "patch_storage",
    "prepare_patch",
    "propose_config_patch",
    "propose_intents",
    "render_intents",
]

_TODO = "plan/patch 미구현: 담당 장민영"


def patch_db_access(inp: object, ctx: RunContext) -> object:
    """patch_db_access: patch_config로 흡수했다(10/3 O3). 등록하지 않는다.

    범위(공통 계약: 설정 주소만, SQL 변환 제외)가 patch_config의 local_address 패턴과 같다.
    코드에 박힌 DB 접속 주소(localhost·127.0.0.1)는 patch_config가 DATABASE_URL 읽기로 고친다.
    """
    raise NotImplementedError(_TODO)


def patch_storage(inp: object, ctx: RunContext) -> object:
    """patch_storage 빈 구현. 입출력은 patch_config와 같은 모양(파일 저장소 패턴)."""
    raise NotImplementedError(_TODO)
