"""ECS 이미지 교체: 요청 형태(Stubber가 실제 API 모델로 검사), 완료 대기. 네트워크 없음."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber

from ddak.cloud.deploy.ecs import EcsService, replace_images, scale_to_zero, wait_stable
from ddak.core.contracts.errors import DdakToolError, ErrorCode

TARGET = EcsService(cluster="ddak", service="flaskr")
OLD_ARN = "arn:aws:ecs:ap-northeast-2:111122223333:task-definition/flaskr:4"
NEW_ARN = "arn:aws:ecs:ap-northeast-2:111122223333:task-definition/flaskr:5"
OLD_WEB = "docker.io/gerbera/flaskr@sha256:" + "1" * 64
OLD_WAS = "docker.io/gerbera/flaskr@sha256:" + "2" * 64
NEW_WEB = "docker.io/gerbera/flaskr@sha256:" + "3" * 64
NEW_WAS = "docker.io/gerbera/flaskr@sha256:" + "4" * 64
SECRET_ARN = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:ddak/flaskr/X-AbCdEf"
CREDS_ARN = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:ddak/dockerhub-ro-AbCdEf"
SVC = {"cluster": "ddak", "services": ["flaskr"]}


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


def _containers(web: str = OLD_WEB, was: str = OLD_WAS) -> list[dict[str, Any]]:
    return [
        {"name": "web", "image": web, "essential": True},
        {
            "name": "was",
            "image": was,
            "essential": True,
            "secrets": [{"name": "SECRET_KEY", "valueFrom": SECRET_ARN}],
            "repositoryCredentials": {"credentialsParameter": CREDS_ARN},
        },
    ]


def _definition(web: str = OLD_WEB, was: str = OLD_WAS) -> dict[str, Any]:
    return {
        "taskDefinitionArn": OLD_ARN,
        "family": "flaskr",
        "revision": 4,
        "status": "ACTIVE",
        "executionRoleArn": "arn:aws:iam::111122223333:role/ddak-flaskr-exec",
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "256",
        "memory": "512",
        "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        "containerDefinitions": _containers(web, was),
    }


def _service(arn: str, desired: int = 2, deployments: list[dict[str, Any]] | None = None) -> dict:
    return {
        "services": [
            {
                "serviceName": "flaskr",
                "status": "ACTIVE",
                "taskDefinition": arn,
                "desiredCount": desired,
                "deployments": deployments or [],
            }
        ]
    }


def _deployment(arn: str, status: str, rollout: str) -> dict[str, Any]:
    return {"taskDefinition": arn, "status": status, "rolloutState": rollout}


def _describe(stub: Stubber, response: dict[str, Any]) -> None:
    stub.add_response("describe_services", response, SVC)


def _read_definition(stub: Stubber, **images: str) -> None:
    stub.add_response(
        "describe_task_definition",
        {"taskDefinition": _definition(**images), "tags": [{"key": "ddak:project", "value": "x"}]},
        {"taskDefinition": OLD_ARN, "include": ["TAGS"]},
    )


def _run(client: Any, images: dict[str, str], **kwargs: Any):
    args: dict[str, Any] = {
        "desired_count": 2,
        "clock": lambda: 0.0,
        "sleep": lambda _s: None,
        "poll_s": 1.0,
    }
    args.update(kwargs)
    return replace_images(client, TARGET, images, 100.0, **args)


def _done(stub: Stubber) -> None:
    _describe(
        stub,
        _service(
            NEW_ARN,
            deployments=[
                _deployment(NEW_ARN, "PRIMARY", "IN_PROGRESS"),
                _deployment(OLD_ARN, "ACTIVE", "COMPLETED"),
            ],
        ),
    )
    _describe(stub, _service(NEW_ARN, deployments=[_deployment(NEW_ARN, "PRIMARY", "COMPLETED")]))


def test_switches_both_containers_in_one_revision(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN))
    _read_definition(stub)
    expected = {
        k: v
        for k, v in _definition().items()
        if k not in ("taskDefinitionArn", "revision", "status")
    }
    expected["containerDefinitions"] = _containers(NEW_WEB, NEW_WAS)
    expected["tags"] = [{"key": "ddak:project", "value": "x"}]
    stub.add_response(
        "register_task_definition", {"taskDefinition": {"taskDefinitionArn": NEW_ARN}}, expected
    )
    _describe(stub, _service(OLD_ARN))
    stub.add_response(
        "update_service",
        {"service": {}},
        {"cluster": "ddak", "service": "flaskr", "taskDefinition": NEW_ARN},
    )
    _done(stub)

    got = _run(client, {"web": NEW_WEB, "was": NEW_WAS})

    assert got.changed is True
    assert got.task_definition == NEW_ARN
    assert got.previous_task_definition == OLD_ARN
    assert got.previous_images == {"web": OLD_WEB, "was": OLD_WAS}


def test_first_deploy_raises_desired_count(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN, desired=0))
    _read_definition(stub)
    stub.add_response(
        "register_task_definition", {"taskDefinition": {"taskDefinitionArn": NEW_ARN}}, None
    )
    _describe(stub, _service(OLD_ARN, desired=0))
    stub.add_response(
        "update_service",
        {"service": {}},
        {"cluster": "ddak", "service": "flaskr", "taskDefinition": NEW_ARN, "desiredCount": 2},
    )
    _done(stub)
    assert _run(client, {"was": NEW_WAS}).changed is True


def test_second_tier_step_is_noop_when_already_switched(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN))
    _read_definition(stub, web=NEW_WEB, was=NEW_WAS)
    _describe(stub, _service(OLD_ARN))
    got = _run(client, {"web": NEW_WEB, "was": NEW_WAS})
    assert got.changed is False
    assert got.task_definition == OLD_ARN


def test_rejects_tag_reference(client: Any) -> None:
    with pytest.raises(DdakToolError) as err:
        _run(client, {"was": "docker.io/gerbera/flaskr:was-r1"})
    assert err.value.code is ErrorCode.CONFIG_INVALID


def test_missing_container_is_config_error(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN))
    stub.add_response(
        "describe_task_definition",
        {"taskDefinition": _definition()},
        {"taskDefinition": ANY, "include": ANY},
    )
    with pytest.raises(DdakToolError) as err:
        _run(client, {"api": NEW_WAS})
    assert err.value.code is ErrorCode.CONFIG_INVALID


def test_missing_service_is_infra_missing(client: Any, stub: Stubber) -> None:
    _describe(stub, {"services": [], "failures": [{"reason": "MISSING"}]})
    with pytest.raises(DdakToolError) as err:
        _run(client, {"was": NEW_WAS})
    assert err.value.code is ErrorCode.INFRA_MISSING


def test_aws_error_hides_original_message(client: Any, stub: Stubber) -> None:
    stub.add_client_error("describe_services", "AccessDeniedException", OLD_ARN)
    with pytest.raises(DdakToolError) as err:
        _run(client, {"was": NEW_WAS})
    assert err.value.code is ErrorCode.ADAPTER_FAILED
    assert "111122223333" not in str(err.value)


def test_wait_fails_on_failed_rollout(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(NEW_ARN, deployments=[_deployment(NEW_ARN, "PRIMARY", "FAILED")]))
    with pytest.raises(DdakToolError) as err:
        wait_stable(client, TARGET, NEW_ARN, 100.0, clock=lambda: 0.0, sleep=lambda _s: None)
    assert err.value.code is ErrorCode.ADAPTER_FAILED


def test_wait_fails_when_another_revision_takes_over(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN, deployments=[_deployment(OLD_ARN, "PRIMARY", "IN_PROGRESS")]))
    with pytest.raises(DdakToolError) as err:
        wait_stable(client, TARGET, NEW_ARN, 100.0, clock=lambda: 0.0, sleep=lambda _s: None)
    assert err.value.code is ErrorCode.ADAPTER_FAILED


def test_wait_times_out(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(NEW_ARN, deployments=[_deployment(NEW_ARN, "PRIMARY", "IN_PROGRESS")]))
    with pytest.raises(DdakToolError) as err:
        wait_stable(client, TARGET, NEW_ARN, 100.0, clock=lambda: 100.0, sleep=lambda _s: None)
    assert err.value.code is ErrorCode.ADAPTER_TIMEOUT


def test_scale_to_zero_skips_stopped_service(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN, desired=0))
    assert scale_to_zero(client, TARGET, 100.0) is False
