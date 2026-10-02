"""C1 등록 계약. 생성 번들은 조립부가 run별 binding으로 전달한다(임시 C-20)."""

from ddak.core.contracts.base import ContractModel, ToolInput
from ddak.core.contracts.enums import Source


class ValidateInfraInput(ToolInput):
    pass


class ValidateInfraOutput(ContractModel):
    passed: bool
    detail: str = ""
    source: Source = Source.LIVE
