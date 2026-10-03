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
    # 두 환경이 똑같이 실패해도 값은 같아 match가 된다. smoke가 통과한 환경끼리만 비교한다.
    # 이것은 패리티 실패(passed=False → 클라우드 롤백)가 아니라 비교 불가(FAILED_VERIFY)다
    failed = [
        t.value for t, out in ((Target.LOCAL, local), (Target.CLOUD, cloud)) if not out.passed
    ]
    if failed:
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED, f"smoke가 통과하지 않은 환경이 있다: {', '.join(failed)}"
        )
    result = compare_env(local, cloud, artifacts=ctx.release_artifacts)
    return CompareEnvResultsOutput(
        run_id=inp.run_id,
        passed=result.passed,
        unexpected_diffs=result.unexpected_diffs,
        checks=[CompareCheck.model_validate(c.to_dict()) for c in result.checks],
        elapsed_s=round(time.monotonic() - started, 3),
        source="live" if local.source == cloud.source == "live" else "fixture",
    )
