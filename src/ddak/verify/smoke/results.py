"""이번 프로세스에서 끝난 smoke_test 결과 보관소. compare_env_results가 읽는다.

실행기는 단계 출력을 다음 툴에 넘기지 않으므로(툴 입력은 plan과 RunContext뿐),
smoke 툴이 결과를 run_id·target별로 남기고 compare 툴이 두 환경 결과를 꺼낸다.
- 실행기는 툴을 같은 프로세스에서 부른다. 프로세스가 바뀌면 결과가 없으므로
  compare는 "비교 불가"로 끝난다.
- 같은 run·target을 다시 돌리면 마지막 결과로 바꾼다. 오래된 run은 MAX_RUNS개를 넘으면 버린다.
- 결과에는 주소·쿠키 값·비밀값이 없다(C-10). 그대로 보관해도 된다.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from ddak.core.contracts.enums import Target
from ddak.core.contracts.tools.smoke_test import SmokeTestOutput

MAX_RUNS = 32

_lock = threading.Lock()
_runs: OrderedDict[str, dict[Target, SmokeTestOutput]] = OrderedDict()


def record(out: SmokeTestOutput) -> None:
    with _lock:
        _runs.setdefault(out.run_id, {})[out.target] = out
        _runs.move_to_end(out.run_id)
        while len(_runs) > MAX_RUNS:
            _runs.popitem(last=False)


def results_for(run_id: str) -> dict[Target, SmokeTestOutput]:
    """run_id의 환경별 smoke 결과 사본. 없으면 빈 dict."""
    with _lock:
        return dict(_runs.get(run_id, {}))


def clear() -> None:
    """테스트용."""
    with _lock:
        _runs.clear()
