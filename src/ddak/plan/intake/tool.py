"""receive_deploy_request 툴 등록(얇은 래퍼)."""

from __future__ import annotations

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.receive_deploy_request import (
    ReceiveDeployRequestInput,
    ReceiveDeployRequestOutput,
)
from ddak.core.registry import tool
from ddak.plan.intake.logic import receive


@tool("receive_deploy_request")
def receive_deploy_request(
    inp: ReceiveDeployRequestInput, ctx: RunContext
) -> ReceiveDeployRequestOutput:
    """GitHub 저장소를 받아 커밋을 고정하고 deploy.yaml을 읽어 소스를 해시한다."""
    return receive(inp, ctx)
