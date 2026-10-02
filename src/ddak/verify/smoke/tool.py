"""smoke_test 레지스트리 연결. 로직 없음: 입력 → 어댑터 선택 → 공통 로직 → 결과 보관 → 출력."""

from __future__ import annotations

from ddak.core.adapters import AdapterSet, select_adapter
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.smoke_test import SmokeTestInput, SmokeTestOutput
from ddak.core.registry import tool
from ddak.verify.smoke.cloud import CloudSmokeAdapter
from ddak.verify.smoke.fake import FakeSmokeAdapter
from ddak.verify.smoke.local import LocalSmokeAdapter
from ddak.verify.smoke.logic import SmokeAdapter, run_smoke
from ddak.verify.smoke.results import record

ADAPTERS: AdapterSet[SmokeAdapter] = AdapterSet(
    local=LocalSmokeAdapter, cloud=CloudSmokeAdapter, fake=FakeSmokeAdapter
)


@tool("smoke_test")
def smoke_test(inp: SmokeTestInput, ctx: RunContext) -> SmokeTestOutput:
    """배포한 앱에 시나리오 묶음을 보내고 환경별 결과를 돌려준다."""
    adapter = select_adapter(ADAPTERS, inp.target, ctx.deploy_config, ctx.adapter_mode)
    out = run_smoke(adapter, inp, ctx)
    record(out)  # compare_env_results가 두 환경 결과를 비교한다
    return out
