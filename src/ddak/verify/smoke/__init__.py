"""verify/smoke: 스모크 테스트. 담당 장민영(O3).

공개 함수: smoke_test, results_for(compare_env_results가 이번 run의 환경별 결과를 읽는다).
다른 디렉토리는 이 파일의 공개 함수만 쓴다.
AI를 import하지 않는다(import-linter 계약). 등록은 tool.py(@tool("smoke_test")).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.smoke_test import SmokeTestInput, SmokeTestOutput
from ddak.verify.smoke.results import results_for


def smoke_test(inp: SmokeTestInput, ctx: RunContext) -> SmokeTestOutput:
    """target 환경의 앱에 inp.scenarios 묶음을 보내고 결과를 돌려준다(C-10)."""
    from ddak.verify.smoke.tool import smoke_test as registered

    return registered(inp, ctx)


__all__ = ["results_for", "smoke_test"]
