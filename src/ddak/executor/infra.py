"""레지스트리 apply 결과를 실행 컨텍스트에 반영한다. AWS 호출은 하지 않는다."""

from dataclasses import replace
from typing import Any

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.infra_outputs import checked_outputs
from ddak.core.contracts.plan import PlanStep
from ddak.core.storage import OUTPUT_KEY


def refresh_infra_context(step: PlanStep, output: dict[str, Any], ctx: RunContext) -> RunContext:
    if step.tool != "apply_infra":
        return ctx
    try:
        if output.get("passed") is not True:
            raise ValueError("apply incomplete")
        safe = checked_outputs(output["outputs"], output["layer"])
        # 기존 flat 설정도 보존한다. nested 값이 있으면 같은 키에 우선한다.
        flat = {k: v for k, v in ctx.platform.items() if k not in {"cloud", "local", "onprem"}}
        cloud = {**flat, **dict(ctx.platform.get("cloud", {})), **safe}
        removing_storage = (
            output["layer"] == "app"
            and (ctx.project_settings.get("_infra_storage") or {}).get("intent") == "remove"
        )
        if removing_storage:
            if OUTPUT_KEY in safe:
                raise ValueError("removed storage output remains")
            cloud.pop(OUTPUT_KEY, None)
    except (KeyError, TypeError, ValueError):
        raise DdakToolError(
            ErrorCode.INFRA_MISSING, "허용된 인프라 출력 갱신 실패", needs_human=True
        ) from None
    platform = {**ctx.platform, "cloud": cloud}
    if removing_storage:
        platform.pop(OUTPUT_KEY, None)
    return replace(ctx, platform=platform)
