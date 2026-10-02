"""build_image: build.<tier> step 하나 = CodeBuild 한 번. 로직은 ddak.cloud.build.image에 있다."""

from __future__ import annotations

from ddak.cloud.build import build_image as run_build_image
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.build_image import BuildImageInput, BuildImageOutput
from ddak.core.registry import tool


@tool("build_image")
def build_image(inp: BuildImageInput, ctx: RunContext) -> BuildImageOutput:
    """tier 이미지를 빌드해 앞선 빌드 결과와 합친 릴리스 산출물을 돌려준다."""
    return run_build_image(inp.tier, ctx)
