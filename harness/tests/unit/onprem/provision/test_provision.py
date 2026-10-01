"""onprem/provision: prepare_host(점검만)와 ensure_app_database 스켈레톤."""

from __future__ import annotations

import pytest

from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.onprem.provision import ensure_app_database, prepare_host

PLATFORM = {
    "onprem": {
        "docker_host": "unix:///var/run/docker.sock",
        "tiers": {
            "was": {"name": "w", "platform": "linux/arm64", "network": "demo-net"},
            "db": {"name": "d", "platform": "linux/arm64"},
        },
    }
}


def test_fake_mode_is_deterministic_and_runs_nothing() -> None:
    calls: list[list[str]] = []
    result = prepare_host(
        RunContext("r", adapter_mode=AdapterMode.FAKE),
        run=lambda argv, t: calls.append(argv) or 1,
    )
    assert result.ok and calls == []
    assert result == prepare_host(RunContext("r", adapter_mode=AdapterMode.FAKE))


def test_real_mode_argv_timeout_and_read_only() -> None:
    calls: list[tuple[list[str], float]] = []

    def run(argv: list[str], timeout: float) -> int:
        calls.append((argv, timeout))
        return 0

    ctx = RunContext("r", adapter_mode=AdapterMode.REAL, platform=PLATFORM)
    result = prepare_host(ctx, run=run)
    host = ["docker", "--host", "unix:///var/run/docker.sock"]
    assert [a for a, _ in calls] == [[*host, "info"], [*host, "network", "inspect", "demo-net"]]
    assert all(0 < t <= 60 for _, t in calls)  # bridge 네트워크는 확인 대상이 아니다
    assert result.ok and [c.name for c in result.checks] == ["docker_info", "network:demo-net"]


def test_real_mode_failure_reports_not_ok() -> None:
    ctx = RunContext("r", adapter_mode=AdapterMode.REAL, platform=PLATFORM)
    result = prepare_host(ctx, run=lambda argv, t: 0 if argv[-1] == "info" else 1)
    assert not result.ok and not result.checks[1].ok


def test_ensure_app_database_stays_unimplemented() -> None:
    with pytest.raises(NotImplementedError, match="onprem/provision 미구현: 담당 김준석"):
        ensure_app_database(RunContext("run-1"))
