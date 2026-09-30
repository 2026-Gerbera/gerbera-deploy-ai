"""plan/dockerfile 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.core.contracts.context import RunContext
from ddak.plan.dockerfile import generate_dockerfile, validate_dockerfile


def test_dockerfile_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="plan/dockerfile 미구현: 담당 장민영"):
        generate_dockerfile(None, RunContext("run-1"))
    with pytest.raises(NotImplementedError, match="plan/dockerfile 미구현: 담당 장민영"):
        validate_dockerfile(None, RunContext("run-1"))
