"""GCP Cloud Run CD provider 자리와 공통 인터페이스 구현."""

from typing import ClassVar

from ddak.cd.interface import ProviderName
from ddak.cloud.deploy.providers._unsupported import UnsupportedCloudProvider


class GcpProvider(UnsupportedCloudProvider):
    name: ClassVar[str] = ProviderName.GCP.value


__all__ = ["GcpProvider"]
