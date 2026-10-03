"""compare_env_results 툴: 실제 레지스트리 + 실행기로 smoke 두 트랙 → 비교까지 이어 본다."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ddak.app import load_tools
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Layer, Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Plan, PlanStep
from ddak.core.contracts.tools.compare_env_results import (
    CompareEnvResultsInput,
    CompareEnvResultsOutput,
)
from ddak.core.contracts.tools.smoke_test import SmokeTestInput
from ddak.executor.engine import Executor, RunStatus
from ddak.verify.compare import compare_env_results
from ddak.verify.smoke import results_for, smoke_test
from ddak.verify.smoke.results import MAX_RUNS, clear, record

RUN = "run-cmp-tool-1"


@pytest.fixture(autouse=True)
def _empty_store() -> Iterator[None]:
    clear()
    yield
    clear()


def _smoke(target: Target, run_id: str = RUN) -> None:
    smoke_test(SmokeTestInput(run_id=run_id, target=target), RunContext(run_id))


def test_compare_is_registered_from_its_directory() -> None:
    tool = load_tools().get("compare_env_results")
    assert tool.input_model is CompareEnvResultsInput
    assert tool.output_model is CompareEnvResultsOutput


def _replace_cloud_value(key: str, value: str) -> None:
    cloud = results_for(RUN)[Target.CLOUD]
    first = cloud.scenarios[0]
    changed = first.model_copy(update={"normalized": {**first.normalized, key: value}})
    record(cloud.model_copy(update={"scenarios": [changed, *cloud.scenarios[1:]]}))


def test_compare_passes_when_both_fake_envs_agree() -> None:
    _smoke(Target.CLOUD)  # 순서와 상관없다
    _smoke(Target.LOCAL)
    _replace_cloud_value("app_env", "cloud")  # 환경이 달라서 다른 것이 정상인 값
    out = compare_env_results(CompareEnvResultsInput(run_id=RUN), RunContext(RUN))
    assert out.passed is True and out.unexpected_diffs == 0
    assert out.source == "fixture"  # fake 어댑터 결과를 비교했으면 목업 라벨
    by_id = {c.id: c for c in out.checks}
    assert by_id["image"].verdict == "skipped"  # 빌드 산출물 없음
    expected = by_id["S0.version.app_env"]
    assert expected.verdict == "expected_diff"
    assert expected.local is None and expected.cloud is None  # 예상된 차이는 값을 싣지 않는다


def test_compare_reports_mismatch() -> None:
    _smoke(Target.LOCAL)
    _smoke(Target.CLOUD)
    _replace_cloud_value("schema_expected", "0002")
    out = compare_env_results(CompareEnvResultsInput(run_id=RUN), RunContext(RUN))
    assert out.passed is False and out.unexpected_diffs == 1
    bad = next(c for c in out.checks if c.verdict == "mismatch")
    assert bad.id == "S0.version.schema_expected" and bad.cloud == "0002"


def test_compare_is_live_only_when_both_results_are_live() -> None:
    _smoke(Target.LOCAL)
    _smoke(Target.CLOUD)
    for target in (Target.LOCAL, Target.CLOUD):
        record(results_for(RUN)[target].model_copy(update={"source": "live"}))
    out = compare_env_results(CompareEnvResultsInput(run_id=RUN), RunContext(RUN))
    assert out.source == "live"


@pytest.mark.parametrize("ran", [[], [Target.LOCAL], [Target.CLOUD]])
def test_compare_without_both_results_is_precondition_failure(ran: list[Target]) -> None:
    for target in ran:
        _smoke(target)
    _smoke(Target.LOCAL, run_id="run-other")  # 다른 run 결과는 쓰지 않는다
    with pytest.raises(DdakToolError) as caught:
        compare_env_results(CompareEnvResultsInput(run_id=RUN), RunContext(RUN))
    assert caught.value.code is ErrorCode.PRECONDITION_FAILED


def test_store_keeps_only_recent_runs() -> None:
    for i in range(MAX_RUNS + 1):
        _smoke(Target.LOCAL, run_id=f"run-old-{i}")
    assert results_for("run-old-0") == {}
    assert Target.LOCAL in results_for(f"run-old-{MAX_RUNS}")


def _step(sid: str, tool: str, **kw: object) -> PlanStep:
    return PlanStep(id=sid, tool=tool, layer=Layer.MANDATORY, **kw)  # type: ignore[arg-type]


def _plan() -> Plan:
    deploy = {
        t: {"steps": [_step(f"verify.smoke.{t}", "smoke_test", signal=f"{t}_verified")]}
        for t in ("local", "cloud")
    }
    return Plan.model_validate(
        {
            "run_id": RUN,
            "deploy": deploy,
            "verify": {
                "steps": [
                    _step(
                        "verify.compare",
                        "compare_env_results",
                        wait_for=["local_verified", "cloud_verified"],
                    )
                ]
            },
        }
    )


@pytest.mark.anyio
async def test_executor_runs_compare_after_both_smoke_tracks() -> None:
    result = await Executor(load_tools()).run(_plan(), RunContext(run_id=RUN))
    assert result.status is RunStatus.SUCCEEDED
    compare = next(r for r in result.records if r.tool == "compare_env_results")
    assert compare.status == "succeeded" and compare.output is not None
    assert compare.output["passed"] is True


@pytest.mark.parametrize("failed", [[Target.LOCAL], [Target.CLOUD], [Target.LOCAL, Target.CLOUD]])
def test_compare_refuses_when_a_smoke_did_not_pass(failed: list[Target]) -> None:
    # 두 환경이 똑같이 실패하면 값이 같아 match가 된다. 패리티 성공이 아니라 비교 불가로 둔다
    _smoke(Target.LOCAL)
    _smoke(Target.CLOUD)
    for target in failed:
        record(results_for(RUN)[target].model_copy(update={"passed": False}))
    with pytest.raises(DdakToolError) as caught:
        compare_env_results(CompareEnvResultsInput(run_id=RUN), RunContext(RUN))
    assert caught.value.code is ErrorCode.PRECONDITION_FAILED
    assert all(t.value in str(caught.value) for t in failed)
