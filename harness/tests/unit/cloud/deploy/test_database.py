"""ECS 일회성 태스크 마이그레이션: 단계 실행, 로그 결과 검증, 실패·시간 초과. 네트워크 없음."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber

from ddak.cloud.deploy.database import MigrationTask, TaskNetwork, run_migration_phases
from ddak.core.contracts.errors import DdakToolError, ErrorCode

TD_ARN = "arn:aws:ecs:ap-northeast-2:111122223333:task-definition/flaskr-was:5"
TASK = MigrationTask(
    cluster="ddak",
    task_definition=TD_ARN,
    container="was",
    network=TaskNetwork(subnets=["subnet-1"], security_groups=["sg-1"]),
)
SIG = "sha256:" + "c" * 64
FP = {
    "version": "8.4",
    "sql_mode": "",
    "collation": "utf8mb4_0900_ai_ci",
    "time_zone": "UTC",
    "ssl_version": "",
}


def _client(name: str) -> Any:
    return boto3.client(
        name,
        region_name="ap-northeast-2",
        aws_access_key_id="test" + "ing",
        aws_secret_access_key="test" + "ing",
    )


@pytest.fixture
def ecs() -> Any:
    return _client("ecs")


@pytest.fixture
def logs() -> Any:
    return _client("logs")


@pytest.fixture
def stubs(ecs: Any, logs: Any) -> Iterator[tuple[Stubber, Stubber]]:
    with Stubber(ecs) as e, Stubber(logs) as lg:
        yield e, lg
        e.assert_no_pending_responses()
        lg.assert_no_pending_responses()


def _result(phase: str, **kw: Any) -> dict[str, Any]:
    base = {
        "phase": phase,
        "ok": True,
        "current": "0002",
        "expected": "0002",
        "applied": ["0002"] if phase == "up" else [],
        "signature": SIG,
        "fingerprint": FP,
    }
    base.update(kw)
    return base


def _definition(e: Stubber, log: bool = True) -> None:
    container: dict[str, Any] = {"name": "was", "image": "docker.io/gerbera/ddak@" + SIG}
    if log:
        container["logConfiguration"] = {
            "logDriver": "awslogs",
            "options": {"awslogs-group": "/ecs/ddak-flaskr", "awslogs-stream-prefix": "ddak"},
        }
    e.add_response(
        "describe_task_definition",
        {"taskDefinition": {"containerDefinitions": [container]}},
        {"taskDefinition": TD_ARN},
    )


COMMAND = [
    "sh",
    "-c",
    "python -m flaskr.migrate precheck --json && python -m flaskr.migrate up --json"
    " && python -m flaskr.migrate verify --json",
]


def _task(
    e: Stubber, lg: Stubber, results: list[dict[str, Any]] | None, exit_code: int = 0
) -> None:
    """태스크 하나가 세 단계를 차례로 돌고 단계마다 MIGRATE_RESULT 한 줄을 남긴다."""
    task_id = "task1"
    e.add_response(
        "run_task",
        {"tasks": [{"taskArn": f"arn:aws:ecs:ap-northeast-2:111122223333:task/ddak/{task_id}"}]},
        {
            "cluster": "ddak",
            "taskDefinition": TD_ARN,
            "launchType": "FARGATE",
            "count": 1,
            "startedBy": "ddak-migrate",
            "networkConfiguration": {
                "awsvpcConfiguration": {
                    "subnets": ["subnet-1"],
                    "securityGroups": ["sg-1"],
                    "assignPublicIp": "ENABLED",
                }
            },
            "overrides": {"containerOverrides": [{"name": "was", "command": COMMAND}]},
        },
    )
    e.add_response(
        "describe_tasks",
        {"tasks": [{"lastStatus": "RUNNING"}]},
        {"cluster": "ddak", "tasks": [task_id]},
    )
    e.add_response(
        "describe_tasks",
        {
            "tasks": [
                {"lastStatus": "STOPPED", "containers": [{"name": "was", "exitCode": exit_code}]}
            ]
        },
        {"cluster": "ddak", "tasks": [task_id]},
    )
    if results is None:
        return
    events = [{"message": "connecting"}]
    events += [{"message": "MIGRATE_RESULT " + json.dumps(r)} for r in results]
    lg.add_response(
        "get_log_events",
        {"events": events, "nextForwardToken": "f/1"},
        {
            "logGroupName": "/ecs/ddak-flaskr",
            "logStreamName": f"ddak/was/{task_id}",
            "startFromHead": True,
        },
    )
    lg.add_response(
        "get_log_events",
        {"events": [], "nextForwardToken": "f/1"},
        {"logGroupName": ANY, "logStreamName": ANY, "startFromHead": True, "nextToken": "f/1"},
    )


def _run(ecs: Any, logs: Any, migrations: list[str], clock: float = 0.0) -> dict[str, Any]:
    return run_migration_phases(
        ecs, logs, TASK, migrations, 100.0, clock=lambda: clock, sleep=lambda _s: None
    )


def test_runs_three_phases_in_one_task_and_returns_onprem_shape(
    ecs: Any, logs: Any, stubs: Any
) -> None:
    e, lg = stubs
    _definition(e)
    _task(e, lg, [_result(p) for p in ("precheck", "up", "verify")])

    got = _run(ecs, logs, ["0002"])

    assert got["event"] == "MIGRATE_RESULT"
    assert got["phase"] == "verify"
    assert [p["phase"] for p in got["phases"]] == ["precheck", "up", "verify"]
    assert got["phases"][1]["applied"] == ["0002"]


def test_verify_mismatch_fails(ecs: Any, logs: Any, stubs: Any) -> None:
    e, lg = stubs
    _definition(e)
    _task(e, lg, [_result("precheck"), _result("up"), _result("verify", current="0001")])
    with pytest.raises(DdakToolError) as err:
        _run(ecs, logs, ["0002"])
    assert err.value.code is ErrorCode.ADAPTER_FAILED


def test_nonzero_exit_fails(ecs: Any, logs: Any, stubs: Any) -> None:
    e, lg = stubs
    _definition(e)
    _task(e, lg, None, exit_code=1)
    with pytest.raises(DdakToolError) as err:
        _run(ecs, logs, ["0002"])
    assert err.value.code is ErrorCode.ADAPTER_FAILED


def test_failed_task_reports_reason_and_partial_results(ecs: Any, logs: Any, stubs: Any) -> None:
    e, lg = stubs
    _definition(e)
    _task(e, lg, [_result("precheck", ok=False, current=None)], exit_code=1)
    with pytest.raises(DdakToolError) as err:
        _run(ecs, logs, ["0002"])
    message = str(err.value)
    assert "exit=1" in message
    assert "precheck ok=False current=None expected=0002" in message


def test_secret_pull_failure_reason_hides_identifiers(ecs: Any, logs: Any, stubs: Any) -> None:
    e, _lg = stubs
    _definition(e)
    arn = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:ddak/flaskr/DB-AbCdEf"
    e.add_response(
        "run_task",
        {"tasks": [{"taskArn": "arn:aws:ecs:ap-northeast-2:111122223333:task/ddak/task1"}]},
        None,
    )
    e.add_response(
        "describe_tasks",
        {
            "tasks": [
                {
                    "lastStatus": "STOPPED",
                    "stoppedReason": f"ResourceInitializationError: unable to pull secrets {arn}",
                    "containers": [{"name": "was"}],
                }
            ]
        },
        None,
    )
    with pytest.raises(DdakToolError) as err:
        _run(ecs, logs, ["0002"])
    message = str(err.value)
    assert "ResourceInitializationError" in message
    assert "111122223333" not in message and "arn:aws" not in message


def test_phase_order_or_not_ok_fails(ecs: Any, logs: Any, stubs: Any) -> None:
    e, lg = stubs
    _definition(e)
    _task(e, lg, [_result("precheck", ok=False), _result("up"), _result("verify")])
    with pytest.raises(DdakToolError) as err:
        _run(ecs, logs, ["0002"])
    assert err.value.code is ErrorCode.ADAPTER_FAILED
    assert "precheck" in str(err.value)


def test_requires_awslogs(ecs: Any, logs: Any, stubs: Any) -> None:
    e, _lg = stubs
    _definition(e, log=False)
    with pytest.raises(DdakToolError) as err:
        _run(ecs, logs, ["0002"])
    assert err.value.code is ErrorCode.CONFIG_INVALID


def test_rejects_bad_migration_id(ecs: Any, logs: Any) -> None:
    with pytest.raises(DdakToolError) as err:
        _run(ecs, logs, ["../x"])
    assert err.value.code is ErrorCode.CONFIG_INVALID


def test_timeout_stops_task(ecs: Any, logs: Any, stubs: Any) -> None:
    e, _lg = stubs
    _definition(e)
    e.add_response(
        "run_task",
        {"tasks": [{"taskArn": "arn:aws:ecs:ap-northeast-2:111122223333:task/ddak/task1"}]},
        None,
    )
    e.add_response("describe_tasks", {"tasks": [{"lastStatus": "RUNNING"}]}, None)
    e.add_response(
        "stop_task", {"task": {}}, {"cluster": "ddak", "task": "task1", "reason": "ddak deadline"}
    )
    with pytest.raises(DdakToolError) as err:
        _run(ecs, logs, ["0002"], clock=100.0)
    assert err.value.code is ErrorCode.ADAPTER_TIMEOUT
