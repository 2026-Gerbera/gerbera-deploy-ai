"""예시 툴 ping: 실제 레지스트리(ddak.app.load_tools) + Fake 어댑터.

툴별 완료 기준(DoD) 예시: 정상 1개 + 실패 1개.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ddak.app import load_tools
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Layer, Target
from ddak.core.contracts.plan import Plan, PlanStep
from ddak.core.contracts.tools.ping import PingInput, PingOutput
from ddak.core.registry import PING
from ddak.executor.engine import Executor, RunStatus

pytestmark = pytest.mark.anyio


def test_ping_is_registered_from_its_directory() -> None:
    registry = load_tools()
    tool = registry.get(PING)
    assert tool.input_model is PingInput
    assert tool.output_model is PingOutput
    assert load_tools() is registry  # 다시 불러도 두 번 등록되지 않는다


@pytest.mark.parametrize("target", [Target.LOCAL, Target.CLOUD])
def test_ping_ok_with_fake_adapter(target: Target) -> None:
    tool = load_tools().get(PING)
    out = tool.fn(PingInput(run_id="run-001", target=target), RunContext(run_id="run-001"))
    assert out == PingOutput(run_id="run-001", target=target, adapter="fake", ok=True)


def test_ping_rejects_unknown_field() -> None:
    # 계약 모델은 extra="forbid". 자유 문자열 명령 같은 입력은 툴 실행 전에 거부된다.
    with pytest.raises(ValidationError):
        PingInput.model_validate({"run_id": "run-001", "target": "local", "cmd": "rm -rf /"})


async def test_ping_runs_on_both_tracks_through_executor() -> None:
    def ping_step(sid: str, **kw: object) -> PlanStep:
        return PlanStep(id=sid, tool=PING, layer=Layer.OPTIONAL, **kw)  # type: ignore[arg-type]

    plan = Plan.model_validate(
        {
            "run_id": "run-001",
            "deploy": {
                "local": {"steps": [ping_step("verify.ping.local", signal="local_verified")]},
                "cloud": {"steps": [ping_step("verify.ping.cloud", wait_for=["local_verified"])]},
            },
        }
    )
    result = await Executor(load_tools()).run(plan, RunContext(run_id="run-001"))
    assert result.status is RunStatus.SUCCEEDED
    assert [r.output["target"] for r in result.records if r.output] == ["local", "cloud"]
