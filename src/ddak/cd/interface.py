"""CD 공통 인터페이스(✅ 9/30: 공통 인터페이스 + provider 모듈). 함수 이름·인자는 💭 초안이다.

provider는 대상 환경 하나(aws = cloud, onprem = local)의 배포 방법을 한 파일에 모은다.
툴(deploy_tier 등)은 provider를 부르기만 하고, 어떤 provider인지는 코드가 target으로 정한다.

| 인터페이스 함수 | 부르는 툴 |
|---|---|
| deploy | deploy_tier |
| rollback | rollback_tier |
| health_check | health_check |
| migrate_db | prepare_db |
| inject_config | inject_env_config(설정) · sync_env_to_cloud(시크릿 값) |
| ensure_tls | ensure_tls (클라우드만. 온프렘은 applicable=False를 돌려준다) |

AWS·GCP·Azure는 cloud/deploy/providers에서 이 Protocol을 구현한다. 실제 동작은 AWS가 우선이며
GCP·Azure는 지원 전까지 명시적인 CONFIG_INVALID 오류를 돌려준다.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import Field

from ddak.core.contracts.base import ContractModel
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.release import ImageObservation

INTERFACE_FUNCTIONS = (
    "deploy",
    "rollback",
    "health_check",
    "migrate_db",
    "inject_config",
    "ensure_tls",
)


class ProviderName(StrEnum):
    """CD provider 이름. 실제 클라우드 동작의 우선순위는 AWS다."""

    AWS = "aws"  # target cloud
    GCP = "gcp"  # target cloud, 인터페이스 구현만
    AZURE = "azure"  # target cloud, 인터페이스 구현만
    ONPREM = "onprem"  # target local


class ProviderResult(ContractModel):
    """provider 함수의 공통 결과. 툴이 자기 출력 모델로 옮겨 담는다."""

    provider: str
    function: str
    applicable: bool = True  # False = 이 환경에는 해당 없음(예: 온프렘 ensure_tls)
    changed: bool = False
    passed: bool = True
    detail: str = Field(default="", max_length=400)  # 비밀값·계정 ID·절대 경로 금지
    observation: ImageObservation | None = None
    previous_image: str | None = None
    image_ref: str | None = None
    migration: dict[str, Any] | None = None
    config_ref: str | None = None  # 값이 아닌 호스트 env 파일 위치
    keys: list[str] = Field(default_factory=list)


class CdProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def target(self) -> Target: ...

    def deploy(self, tier: str, ctx: RunContext) -> ProviderResult: ...

    def rollback(self, tier: str, ctx: RunContext) -> ProviderResult: ...

    def health_check(self, ctx: RunContext) -> ProviderResult: ...

    def migrate_db(self, migrations: Sequence[str], ctx: RunContext) -> ProviderResult: ...

    def inject_config(self, keys: Sequence[str], ctx: RunContext) -> ProviderResult: ...

    def ensure_tls(self, mode: Literal["check", "apply"], ctx: RunContext) -> ProviderResult: ...
