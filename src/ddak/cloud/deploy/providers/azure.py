"""Azure Container Apps CD provider 자리와 공통 인터페이스 구현."""

from typing import ClassVar

from ddak.cd.interface import ProviderName
from ddak.cloud.deploy.providers._unsupported import UnsupportedCloudProvider


class AzureProvider(UnsupportedCloudProvider):
    name: ClassVar[str] = ProviderName.AZURE.value


__all__ = ["AzureProvider"]
