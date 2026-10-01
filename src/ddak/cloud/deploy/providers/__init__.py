"""클라우드별 CD provider와 선택 함수.

공통 CD 인터페이스는 ``ddak.cd.interface.CdProvider`` 하나를 사용하고, CSP 차이는
이 패키지 안에 가둔다. 현재 실제 배포 구현 대상은 AWS이며 GCP와 Azure는 같은 인터페이스를
구현하는 명시적 미지원 provider다.
"""

from __future__ import annotations

from collections.abc import Mapping

from ddak.cd.interface import CdProvider, ProviderName
from ddak.cloud.deploy.providers.aws import AwsProvider
from ddak.cloud.deploy.providers.azure import AzureProvider
from ddak.cloud.deploy.providers.gcp import GcpProvider
from ddak.core.contracts.errors import DdakToolError, ErrorCode

_PROVIDERS: Mapping[ProviderName, type[CdProvider]] = {
    ProviderName.AWS: AwsProvider,
    ProviderName.GCP: GcpProvider,
    ProviderName.AZURE: AzureProvider,
}


def cloud_provider(name: ProviderName | str = ProviderName.AWS) -> CdProvider:
    """provider 이름을 검증하고 해당 클라우드 구현을 돌려준다."""
    try:
        provider_name = ProviderName(name)
        provider_type = _PROVIDERS[provider_name]
    except (ValueError, KeyError) as exc:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "지원하지 않는 cloud provider다") from exc
    return provider_type()


__all__ = ["AwsProvider", "AzureProvider", "GcpProvider", "cloud_provider"]
