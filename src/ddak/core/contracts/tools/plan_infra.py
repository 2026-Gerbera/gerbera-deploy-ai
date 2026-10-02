from typing import Any

from ddak.core.contracts.base import ContractModel, ToolInput
from ddak.core.contracts.enums import Source
from ddak.core.contracts.release import Sha256


class PlanInfraInput(ToolInput):
    pass


class PlanInfraOutput(ContractModel):
    passed: bool
    plan_sha256: Sha256
    summary: dict[str, Any]
    source: Source = Source.LIVE
