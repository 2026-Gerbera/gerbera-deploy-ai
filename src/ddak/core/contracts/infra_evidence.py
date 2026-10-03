"""권한 경계 버전 변경의 값 없는 복구 근거."""

from typing import Literal

from pydantic import Field

from ddak.core.contracts.base import ContractModel

_VERSION = r"^v[1-9][0-9]*(\.[A-Za-z0-9-]*)?$"


class BoundaryPolicyVersion(ContractModel):
    policy_arn: str = Field(
        pattern=r"^arn:aws:iam::\*{12}:policy/ddak/boundary/ddak-(app|build)-boundary$"
    )
    previous_version_id: str | None = Field(default=None, pattern=_VERSION)
    new_version_id: str | None = Field(default=None, pattern=_VERSION)
    status: Literal["created", "updated", "unchanged", "unknown"]
