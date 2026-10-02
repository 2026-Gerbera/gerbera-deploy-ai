"""ECS 이미지 교체: 요청 형태(Stubber가 실제 API 모델로 검사), 안정화 대기. 네트워크 없음."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber

from ddak.cloud.deploy.ecs import EcsService, replace_image, wait_stable
from ddak.core.contracts.errors import DdakToolError, ErrorCode

TARGET = EcsService(cluster="ddak", service="flaskr-was", container="was")
OLD_ARN = "arn:aws:ecs:ap-northeast-2:111122223333:task-definition/flaskr-was:4"
NEW_ARN = "arn:aws:ecs:ap-northeast-2:111122223333:task-definition/flaskr-was:5"
OLD_IMAGE = "docker.io/gerbera/ddak@sha256:" + "1" * 64
NEW_IMAGE = "docker.io/gerbera/ddak@sha256:" + "2" * 64
SECRET_ARN = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:ddak/flaskr/app-AbCdEf"
CREDS_ARN = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:ddak/dockerhub-ro-AbCdEf"


@pytest.fixture
def client() -> Any:
    return boto3.client(
        "ecs",
        region_name="ap-northeast-2",
        aws_access_key_id="test" + "ing",
        aws_secret_access_key="test" + "ing",
    )


@pytest.fixture
def stub(client: Any) -> Iterator[Stubber]:
    with Stubber(client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def _containers(image: str = OLD_IMAGE) -> list[dict[str, Any]]:
    return [
        {
            "name": "was",
            "image": image,
            "essential": True,
            "secrets": [{"name": "SECRET_KEY", "valueFrom": SECRET_ARN}],
            "repositoryCredentials": {"credentialsParameter": CREDS_ARN},
        },
        {"name": "log-router", "image": "public.ecr.aws/aws-observability/aws-for-fluent-bit:3"},
    ]


def _definition(image: str = OLD_IMAGE) -> dict[str, Any]:
    return {
        "taskDefinitionArn": OLD_ARN,
        "family": "flaskr-was",
        "revision": 4,
        "status": "ACTIVE",
        "executionRoleArn": "arn:aws:iam::111122223333:role/ddak-flaskr-exec",
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "256",
        "memory": "512",
        "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        "containerDefinitions": _containers(image),
    }


def _service(task_definition: str, deployments: list[dict[str, Any]] | None = None) -> dict:
    return {
        "services": [
            {
                "serviceName": "flaskr-was",
                "status": "ACTIVE",
                "taskDefinition": task_definition,
                "deployments": deployments or [],
            }
        ]
    }


def _deployment(arn: str, status: str, rollout: str) -> dict[str, Any]:
    return {"taskDefinition": arn, "status": status, "rolloutState": rollout}


def _describe(stub: Stubber, response: dict[str, Any]) -> None:
    stub.add_response(
        "describe_services", response, {"cluster": "ddak", "services": ["flaskr-was"]}
    )


def _run(client: Any, image: str = NEW_IMAGE, **kwargs: Any):
    args: dict[str, Any] = {"clock": lambda: 0.0, "sleep": lambda _s: None, "poll_s": 1.0}
    args.update(kwargs)
    return replace_image(client, TARGET, image, 100.0, **args)


def test_registers_copy_with_new_image_and_waits(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN))
    stub.add_response(
        "describe_task_definition",
        {"taskDefinition": _definition(), "tags": [{"key": "ddak:project", "value": "flaskr"}]},
        {"taskDefinition": OLD_ARN, "include": ["TAGS"]},
    )
    expected = {
        k: v
        for k, v in _definition().items()
        if k not in ("taskDefinitionArn", "revision", "status")
    }
    expected["containerDefinitions"] = _containers(NEW_IMAGE)
    expected["tags"] = [{"key": "ddak:project", "value": "flaskr"}]
    stub.add_response(
        "register_task_definition", {"taskDefinition": {"taskDefinitionArn": NEW_ARN}}, expected
    )
    stub.add_response(
        "update_service",
        {"service": {"serviceName": "flaskr-was"}},
        {"cluster": "ddak", "service": "flaskr-was", "taskDefinition": NEW_ARN},
    )
    _describe(
        stub,
        _service(
            NEW_ARN,
            [
                _deployment(NEW_ARN, "PRIMARY", "IN_PROGRESS"),
                _deployment(OLD_ARN, "ACTIVE", "COMPLETED"),
            ],
        ),
    )
    _describe(stub, _service(NEW_ARN, [_deployment(NEW_ARN, "PRIMARY", "COMPLETED")]))

    got = _run(client)

    assert got.changed is True
    assert got.task_definition == NEW_ARN
    assert got.previous_task_definition == OLD_ARN
    assert got.previous_image == OLD_IMAGE


def test_same_image_does_nothing(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN))
    stub.add_response(
        "describe_task_definition",
        {"taskDefinition": _definition()},
        {"taskDefinition": OLD_ARN, "include": ["TAGS"]},
    )
    got = _run(client, OLD_IMAGE)
    assert got.changed is False
    assert got.task_definition == OLD_ARN


def test_rejects_tag_reference(client: Any) -> None:
    with pytest.raises(DdakToolError) as err:
        _run(client, "docker.io/gerbera/ddak:was-r1")
    assert err.value.code is ErrorCode.CONFIG_INVALID


def test_missing_container_is_config_error(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN))
    definition = _definition()
    definition["containerDefinitions"] = definition["containerDefinitions"][1:]
    stub.add_response(
        "describe_task_definition",
        {"taskDefinition": definition},
        {"taskDefinition": ANY, "include": ANY},
    )
    with pytest.raises(DdakToolError) as err:
        _run(client)
    assert err.value.code is ErrorCode.CONFIG_INVALID


def test_missing_service_is_infra_missing(client: Any, stub: Stubber) -> None:
    _describe(stub, {"services": [], "failures": [{"reason": "MISSING"}]})
    with pytest.raises(DdakToolError) as err:
        _run(client)
    assert err.value.code is ErrorCode.INFRA_MISSING


def test_aws_error_hides_original_message(client: Any, stub: Stubber) -> None:
    stub.add_client_error("describe_services", "AccessDeniedException", OLD_ARN)
    with pytest.raises(DdakToolError) as err:
        _run(client)
    assert err.value.code is ErrorCode.ADAPTER_FAILED
    assert "111122223333" not in str(err.value)


def test_wait_fails_on_circuit_breaker(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(NEW_ARN, [_deployment(NEW_ARN, "PRIMARY", "FAILED")]))
    with pytest.raises(DdakToolError) as err:
        wait_stable(client, TARGET, NEW_ARN, 100.0, clock=lambda: 0.0, sleep=lambda _s: None)
    assert err.value.code is ErrorCode.ADAPTER_FAILED


def test_wait_fails_when_another_revision_takes_over(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN, [_deployment(OLD_ARN, "PRIMARY", "IN_PROGRESS")]))
    with pytest.raises(DdakToolError) as err:
        wait_stable(client, TARGET, NEW_ARN, 100.0, clock=lambda: 0.0, sleep=lambda _s: None)
    assert err.value.code is ErrorCode.ADAPTER_FAILED


def test_wait_times_out(client: Any, stub: Stubber) -> None:
    in_progress = _service(NEW_ARN, [_deployment(NEW_ARN, "PRIMARY", "IN_PROGRESS")])
    _describe(stub, in_progress)
    with pytest.raises(DdakToolError) as err:
        wait_stable(client, TARGET, NEW_ARN, 100.0, clock=lambda: 100.0, sleep=lambda _s: None)
    assert err.value.code is ErrorCode.ADAPTER_TIMEOUT
