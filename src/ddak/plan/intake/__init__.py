"""plan/intake: 배포 요청 접수(GitHub 가져오기·커밋 고정·deploy.yaml·소스 해시).
담당 정준우(O1, O2 승계).

공개 이름: receive_deploy_request, cleanup_stale_sources, FetchPolicy, WatchTarget, Watcher,
load_watch_targets, resolve_head, warm_cache.
AI를 import하지 않는다(import-linter 계약). tool.py는 여기서 import하지 않는다(ddak.app이 탐색).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.receive_deploy_request import (
    ReceiveDeployRequestInput,
    ReceiveDeployRequestOutput,
)
from ddak.plan.intake.fetch import resolve_head, warm_cache
from ddak.plan.intake.logic import Fetcher, cleanup_stale_sources, receive
from ddak.plan.intake.policy import FetchPolicy
from ddak.plan.intake.watch import (
    Watcher,
    WatchTarget,
    initial_trigger_from_env,
    interval_from_env,
    load_watch_targets,
)

__all__ = [
    "FetchPolicy",
    "WatchTarget",
    "Watcher",
    "cleanup_stale_sources",
    "initial_trigger_from_env",
    "interval_from_env",
    "load_watch_targets",
    "receive_deploy_request",
    "resolve_head",
    "warm_cache",
]


def receive_deploy_request(
    inp: ReceiveDeployRequestInput,
    ctx: RunContext,
    *,
    policy: FetchPolicy | None = None,
    fetcher: Fetcher | None = None,
) -> ReceiveDeployRequestOutput:
    """policy·fetcher는 호출자(flow)·테스트 주입용. 없으면 환경변수 정책과 GitHub fetch."""
    if fetcher is None:
        return receive(inp, ctx, policy=policy)
    return receive(inp, ctx, policy=policy, fetcher=fetcher)
