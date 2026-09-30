"""CD provider 선택. target과 어댑터 모드(코드)로만 고른다. AI가 고르지 않는다."""

from __future__ import annotations

from ddak.cd.interface import CdProvider
from ddak.cd.providers.aws import AwsProvider
from ddak.cd.providers.fake import FakeProvider
from ddak.cd.providers.onprem import OnPremProvider
from ddak.core.config import AdapterMode
from ddak.core.contracts.enums import Target


def select_provider(target: Target, mode: AdapterMode) -> CdProvider:
    if mode is AdapterMode.FAKE:
        return FakeProvider(target)
    return OnPremProvider() if target is Target.LOCAL else AwsProvider()


__all__ = ["AwsProvider", "FakeProvider", "OnPremProvider", "select_provider"]
