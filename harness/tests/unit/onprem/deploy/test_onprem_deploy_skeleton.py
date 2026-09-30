"""onprem/deploy 공개 이름: import되고 CD 인터페이스 구현을 노출한다(빈 구현은 health_check뿐)."""

from __future__ import annotations

import pytest

import ddak.onprem.deploy.config
import ddak.onprem.deploy.migrate
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError
from ddak.onprem.deploy import DockerHost, OnPremProvider


def test_onprem_deploy_skeleton_exposes_provider() -> None:
    assert OnPremProvider.__module__ == "ddak.onprem.deploy.provider"
    assert DockerHost.__module__ == "ddak.onprem.deploy.containers"
    assert ddak.onprem.deploy.config.__doc__
    assert ddak.onprem.deploy.migrate.__doc__
    # health_check는 O3 구현을 주입받기 전까지 미구현 오류(DdakToolError)다.
    with pytest.raises(DdakToolError):
        OnPremProvider().health_check(RunContext("run-1"))
