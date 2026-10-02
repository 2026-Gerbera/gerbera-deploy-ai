"""compare_env_results 레지스트리 연결. 로직 없음: 이번 run의 두 환경 결과 → 비교 로직 → 출력."""

from __future__ import annotations

import time

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.compare_env_results import (
    CompareCheck,
    CompareEnvResultsInput,
    CompareEnvResultsOutput,
)
from ddak.core.registry import tool
from ddak.verify.compare.logic import compare_env
from ddak.verify.smoke import results_for


@tool("compare_env_results")
def compare_env_results(inp: CompareEnvResultsInput, ctx: RunContext) -> CompareEnvResultsOutput:
    """두 환경의 smoke 결과·이미지 관측을 비교한다. 결과가 한쪽이라도 없으면 비교 불가."""
    started = time.monotonic()
    smoke = results_for(inp.run_id)
    missing = [t.value for t in (Target.LOCAL, Target.CLOUD) if t not in smoke]
    if missing:
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED, f"이번 run의 smoke 결과가 없다: {', '.join(missing)}"
        )
    local, cloud = smoke[Target.LOCAL], smoke[Target.CLOUD]
    result = compare_env(local, cloud, artifacts=ctx.release_artifacts)
    return CompareEnvResultsOutput(
        run_id=inp.run_id,
        passed=result.passed,
        unexpected_diffs=result.unexpected_diffs,
        checks=[CompareCheck.model_validate(c.to_dict()) for c in result.checks],
        elapsed_s=round(time.monotonic() - started, 3),
        source="live" if local.source == cloud.source == "live" else "fixture",
    )
