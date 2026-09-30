"""cloud/deploy 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.cloud.deploy import (
    AwsProvider,
    deploy_service,
    put_secret_values,
    rollback_service,
    run_migrations,
)
from ddak.core.contracts.context import RunContext


def test_cloud_deploy_skeleton_raises_not_implemented() -> None:
    assert AwsProvider.__module__ == "ddak.cloud.deploy.provider"
    with pytest.raises(NotImplementedError, match="cloud/deploy 미구현: 담당 안승환"):
        deploy_service("was", RunContext("run-1"))
    with pytest.raises(NotImplementedError, match="cloud/deploy 미구현: 담당 안승환"):
        rollback_service("was", RunContext("run-1"))
    with pytest.raises(NotImplementedError, match="cloud/deploy 미구현: 담당 안승환"):
        put_secret_values(["SECRET_KEY"], RunContext("run-1"))
    with pytest.raises(NotImplementedError, match="cloud/deploy 미구현: 담당 안승환"):
        run_migrations(["0001"], RunContext("run-1"))
