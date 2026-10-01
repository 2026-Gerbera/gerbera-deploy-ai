"""plan/intake: 배포 요청 접수(GitHub 가져오기·커밋 고정·deploy.yaml·소스 해시). 담당 김준석(O2).

공개 이름: receive_deploy_request, cleanup_stale_sources, FetchPolicy.
AI를 import하지 않는다(import-linter 계약). tool.py는 여기서 import하지 않는다(ddak.app이 탐색).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.receive_deploy_request import (
    ReceiveDeployRequestInput,
    ReceiveDeployRequestOutput,
)
from ddak.plan.intake.logic import cleanup_stale_sources, receive
from ddak.plan.intake.policy import FetchPolicy

__all__ = ["FetchPolicy", "cleanup_stale_sources", "receive_deploy_request"]


def receive_deploy_request(
    inp: ReceiveDeployRequestInput, ctx: RunContext
) -> ReceiveDeployRequestOutput:
    return receive(inp, ctx)
