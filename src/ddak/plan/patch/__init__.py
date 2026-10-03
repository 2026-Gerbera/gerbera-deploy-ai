"""plan/patch: AI 코드 수정 P0(새 AI 제안은 토글 ON, OFF는 이전 패치 재적용만). 담당 장민영(O3).

공개 함수: patch_config(툴 진입점, tool.py가 등록),
patch_db_access·patch_storage(빈 구현, 등록 안 함),
propose_config_patch(patch_config 제안 로직, generate.py).
다른 디렉토리는 이 파일의 공개 이름만 쓴다.
AI 호출은 ddak.core.ai(call_ai, ask_jev)로만 한다(허용 디렉토리, import-linter 계약 2).
patch_config 입출력은 core/contracts/tools/patch_config.py(초안, 정준우 승인 대기).
계획 흐름은 tool_context("patch_config", run_id) 안에서 patch_config를 부르고,
passed=True일 때 patch(UTF-8로 인코딩)·meta를 실행기 prepare(patch=, patch_meta=)에 넘긴다.
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
)

__all__ = [
    "PatchDraft",
    "PatchEdit",
    "PatchProposal",
    "PreviousPatch",
    "find_targets",
    "patch_config",
    "patch_db_access",
    "patch_storage",
    "propose_config_patch",
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
