"""patch_config 툴 등록(얇은 래퍼). AI는 tool_context("patch_config") 안에서만 허용된다."""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.patch_config import PatchConfigInput, PatchConfigOutput
from ddak.core.registry import tool
from ddak.plan.patch import patch_config as _patch_config


@tool("patch_config")
def patch_config(inp: PatchConfigInput, ctx: RunContext) -> PatchConfigOutput:
    """토글 code_patch가 켜진 run에서 prod 원본에 맞는 설정 패치를 제안·검사한다."""
    return _patch_config(inp, ctx)
