"""CD 공통 인터페이스 + provider 모듈(✅ 9/30). provider 구현은 TODO라 모양만 확인한다."""

from __future__ import annotations

import importlib.util
import inspect
from collections.abc import Callable

import pytest

from ddak.cd.dispatch import AwsProvider, FakeProvider, OnPremProvider, select_provider
from ddak.cd.interface import INTERFACE_FUNCTIONS, CdProvider, ProviderName
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError


def _params(fn: Callable[..., object]) -> list[str]:
    return [p for p in inspect.signature(fn).parameters if p != "self"]


@pytest.mark.parametrize("cls", [AwsProvider, OnPremProvider, FakeProvider])
def test_providers_implement_all_interface_functions(cls: type) -> None:
    for name in INTERFACE_FUNCTIONS:
        proto, impl = getattr(CdProvider, name), getattr(cls, name, None)
        assert callable(impl), f"{cls.__name__}.{name} 없음"
        assert _params(impl) == _params(proto), f"{cls.__name__}.{name} 인자가 인터페이스와 다르다"


def test_provider_targets() -> None:
    assert AwsProvider.target is Target.CLOUD
    assert OnPremProvider.target is Target.LOCAL
    assert {p.value for p in ProviderName} == {"aws", "onprem"}


def test_onprem_ensure_tls_is_not_applicable_not_a_failure() -> None:
    result = OnPremProvider().ensure_tls("check", RunContext("run-1"))
    assert result.applicable is False
    assert result.function == "ensure_tls"


def test_unimplemented_provider_functions_raise_tool_error() -> None:
    with pytest.raises(DdakToolError):
        AwsProvider().deploy("was", RunContext("run-1"))


def test_select_provider_uses_target_and_mode_only() -> None:
    assert isinstance(select_provider(Target.LOCAL, AdapterMode.REAL), OnPremProvider)
    assert isinstance(select_provider(Target.CLOUD, AdapterMode.REAL), AwsProvider)
    fake = select_provider(Target.CLOUD, AdapterMode.FAKE)
    assert isinstance(fake, FakeProvider)
    assert fake.target is Target.CLOUD
    assert fake.ensure_tls("check", RunContext("run-1")).applicable is True
    local = select_provider(Target.LOCAL, AdapterMode.FAKE)
    assert local.ensure_tls("check", RunContext("run-1")).applicable is False


def test_gcp_and_azure_have_no_provider_code_yet() -> None:
    # CSP 우선순위 AWS 1순위, GCP·Azure는 후순위(인터페이스만, 코드 없음, ✅ 9/30).
    # provider는 환경별 팀 디렉토리(cloud/deploy, onprem/deploy)와 cd/fake.py에만 있다.
    assert AwsProvider.__module__ == "ddak.cloud.deploy.provider"
    assert OnPremProvider.__module__ == "ddak.onprem.deploy.provider"
    assert FakeProvider.__module__ == "ddak.cd.fake"
    for name in ("ddak.cd.providers", "ddak.cloud.gcp", "ddak.cloud.azure"):
        assert importlib.util.find_spec(name) is None, name
