from typing import Any, Literal, Self

from pydantic import Field, model_validator

from ddak.core.contracts.base import ContractModel, LockToken, ToolInput
from ddak.core.contracts.enums import Source
from ddak.core.contracts.infra_outputs import checked_outputs
from ddak.core.contracts.release import Sha256


class ApplyInfraInput(ToolInput):
    lock_token: LockToken


class ApplyInfraOutput(ContractModel):
    passed: bool
    layer: Literal["app", "platform"]
    plan_sha256: Sha256
    outputs: dict[str, Any]
    elapsed_seconds: float = Field(ge=0)
    source: Source = Source.LIVE

    @model_validator(mode="after")
    def safe_outputs(self) -> Self:
        checked_outputs(self.outputs, self.layer)
        return self
