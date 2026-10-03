"""ECS 이미지 교체: 요청 형태(Stubber가 실제 API 모델로 검사), 완료 대기. 네트워크 없음."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber

from ddak.cloud.deploy.ecs import (
    EcsService,
    replace_images,
    rollback_in_flight,
    scale_to_zero,
    wait_stable,
)
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


DB_ARN = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:ddak/flaskr/DB-AbCdEf"
ENV = {"was": {"APP_ENV": "production", "RELEASE_ID": "run-1"}, "web": {"WAS_UPSTREAM": "x:1"}}
SECRETS = {"was": {"DATABASE_URL": DB_ARN}}


def _settled(containers: list[dict[str, Any]], release_id: str = "run-1") -> list[dict[str, Any]]:
    """ENV·SECRETS(RELEASE_ID만 바꿔)가 이미 들어간 컨테이너."""
    for c in containers:
        values = {**ENV[c["name"]]}
        if "RELEASE_ID" in values:
            values["RELEASE_ID"] = release_id
        c["environment"] = [{"name": k, "value": v} for k, v in sorted(values.items())]
        if c["name"] in SECRETS:
            c["secrets"] = [
                {"name": k, "valueFrom": v} for k, v in sorted(SECRETS[c["name"]].items())
            ]
    return containers


def test_runtime_settings_replace_each_named_container(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN))
    definition = _definition()
    definition["containerDefinitions"][1]["environment"] = [  # 이전 값은 남기지 않는다
        {"name": "STALE", "value": "x"},
    ]
    stub.add_response(
        "describe_task_definition",
        {"taskDefinition": definition},
        {"taskDefinition": OLD_ARN, "include": ["TAGS"]},
    )
    expected = {
        k: v
        for k, v in _definition().items()
        if k not in ("taskDefinitionArn", "revision", "status")
    }
    expected["containerDefinitions"] = _settled(_containers(NEW_WEB, NEW_WAS))
    stub.add_response(
        "register_task_definition", {"taskDefinition": {"taskDefinitionArn": NEW_ARN}}, expected
    )
    _describe(stub, _service(OLD_ARN))
    stub.add_response("update_service", {"service": {}}, None)
    _done(stub)
    got = _run(client, {"web": NEW_WEB, "was": NEW_WAS}, environment=ENV, secrets=SECRETS)
    assert got.changed is True


def test_noop_needs_same_images_and_runtime_settings(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN))
    definition = _definition(NEW_WEB, NEW_WAS)
    _settled(definition["containerDefinitions"])
    stub.add_response("describe_task_definition", {"taskDefinition": definition}, None)
    _describe(stub, _service(OLD_ARN))
    got = _run(client, {"web": NEW_WEB, "was": NEW_WAS}, environment=ENV, secrets=SECRETS)
    assert got.changed is False


def test_same_images_with_new_release_id_registers_revision(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN))
    definition = _definition(NEW_WEB, NEW_WAS)
    _settled(definition["containerDefinitions"], release_id="run-0")
    stub.add_response("describe_task_definition", {"taskDefinition": definition}, None)
    stub.add_response(
        "register_task_definition", {"taskDefinition": {"taskDefinitionArn": NEW_ARN}}, None
    )
    _describe(stub, _service(OLD_ARN))
    stub.add_response("update_service", {"service": {}}, None)
    _done(stub)
    got = _run(client, {"web": NEW_WEB, "was": NEW_WAS}, environment=ENV, secrets=SECRETS)
    assert got.changed is True


@pytest.mark.parametrize("env", [{"release_id": "x"}, {"RELEASE_ID": ""}, {"RELEASE_ID": "a\nb"}])
def test_rejects_bad_environment(client: Any, env: dict[str, str]) -> None:
    with pytest.raises(DdakToolError) as err:
        _run(client, {"was": NEW_WAS}, environment={"was": env})
    assert err.value.code is ErrorCode.CONFIG_INVALID


def test_rejects_settings_for_unknown_container(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN))
    stub.add_response("describe_task_definition", {"taskDefinition": _definition()}, None)
    with pytest.raises(DdakToolError) as err:
        _run(client, {"was": NEW_WAS}, environment={"worker": {"A": "1"}})
    assert err.value.code is ErrorCode.CONFIG_INVALID


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


# ---- 블루그린(10/3 정준우 결정: ECS 네이티브 BLUE_GREEN, bake 1분) ----

DEPLOY_ARN = "arn:aws:ecs:ap-northeast-2:111122223333:service-deployment/ddak/flaskr/abc"
ROLLING_BACK = ["ROLLBACK_REQUESTED", "ROLLBACK_IN_PROGRESS"]
ACTIVE = ["PENDING", "IN_PROGRESS"]


def _bg_service(arn: str = OLD_ARN, desired: int = 2) -> dict[str, Any]:
    service = _service(arn, desired)
    service["services"][0]["deploymentConfiguration"] = {
        "strategy": "BLUE_GREEN",
        "bakeTimeInMinutes": 1,
    }
    return service


def _list(stub: Stubber, statuses: list[str], found: list[dict[str, Any]] | None = None) -> None:
    stub.add_response(
        "list_service_deployments",
        {"serviceDeployments": found or []},
        {"cluster": "ddak", "service": "flaskr", "status": statuses},
    )


def _deploy_state(stub: Stubber, status: str, stage: str | None = None) -> None:
    item: dict[str, Any] = {"serviceDeploymentArn": DEPLOY_ARN, "status": status}
    if stage:
        item["lifecycleStage"] = stage
    stub.add_response(
        "describe_service_deployments",
        {"serviceDeployments": [item]},
        {"serviceDeploymentArns": [DEPLOY_ARN]},
    )


def _bg_update(stub: Stubber) -> None:
    stub.add_response("update_service", {"service": {"currentServiceDeployment": DEPLOY_ARN}}, None)


def test_blue_green_completes_when_production_traffic_shifted(client: Any, stub: Stubber) -> None:
    """BAKE_TIME 진입(운영 트래픽 전환 완료)이 완료 시점이다. 해제 지연을 기다리지 않는다."""
    _describe(stub, _bg_service())
    _read_definition(stub)
    stub.add_response(
        "register_task_definition", {"taskDefinition": {"taskDefinitionArn": NEW_ARN}}, None
    )
    _describe(stub, _bg_service())
    _list(stub, ROLLING_BACK)
    _bg_update(stub)
    _deploy_state(stub, "IN_PROGRESS", "SCALE_UP")
    _deploy_state(stub, "IN_PROGRESS", "PRODUCTION_TRAFFIC_SHIFT")
    _deploy_state(stub, "IN_PROGRESS", "BAKE_TIME")
    got = _run(client, {"web": NEW_WEB, "was": NEW_WAS})
    assert got.changed is True
    assert got.task_definition == NEW_ARN


@pytest.mark.parametrize("status", ["ROLLBACK_IN_PROGRESS", "STOPPED", "ROLLBACK_SUCCESSFUL"])
def test_blue_green_failure_or_rollback_fails(client: Any, stub: Stubber, status: str) -> None:
    _describe(stub, _bg_service())
    _read_definition(stub)
    stub.add_response(
        "register_task_definition", {"taskDefinition": {"taskDefinitionArn": NEW_ARN}}, None
    )
    _describe(stub, _bg_service())
    _list(stub, ROLLING_BACK)
    _bg_update(stub)
    _deploy_state(stub, status, "SCALE_UP")
    with pytest.raises(DdakToolError) as err:
        _run(client, {"web": NEW_WEB, "was": NEW_WAS})
    assert err.value.code is ErrorCode.ADAPTER_FAILED


def test_blue_green_waits_for_previous_rollback_before_deploying(
    client: Any, stub: Stubber
) -> None:
    _describe(stub, _bg_service())
    _read_definition(stub)
    stub.add_response(
        "register_task_definition", {"taskDefinition": {"taskDefinitionArn": NEW_ARN}}, None
    )
    _describe(stub, _bg_service())
    _list(
        stub, ROLLING_BACK, [{"serviceDeploymentArn": "arn:old", "status": "ROLLBACK_IN_PROGRESS"}]
    )
    _list(stub, ROLLING_BACK)  # 끝남 → 이제 배포
    _bg_update(stub)
    _deploy_state(stub, "SUCCESSFUL")
    assert _run(client, {"web": NEW_WEB, "was": NEW_WAS}).changed is True


def test_blue_green_timeout(client: Any, stub: Stubber) -> None:
    _describe(stub, _bg_service())
    _read_definition(stub)
    stub.add_response(
        "register_task_definition", {"taskDefinition": {"taskDefinitionArn": NEW_ARN}}, None
    )
    _describe(stub, _bg_service())
    _list(stub, ROLLING_BACK)
    _bg_update(stub)
    _deploy_state(stub, "IN_PROGRESS", "SCALE_UP")
    with pytest.raises(DdakToolError) as err:
        _run(client, {"web": NEW_WEB, "was": NEW_WAS}, clock=lambda: 200.0)
    assert err.value.code is ErrorCode.ADAPTER_TIMEOUT


def test_default_poll_interval_is_two_seconds(client: Any, stub: Stubber) -> None:
    sleeps: list[float] = []
    _describe(stub, _bg_service())
    _read_definition(stub)
    stub.add_response(
        "register_task_definition", {"taskDefinition": {"taskDefinitionArn": NEW_ARN}}, None
    )
    _describe(stub, _bg_service())
    _list(stub, ROLLING_BACK)
    _bg_update(stub)
    _deploy_state(stub, "IN_PROGRESS", "SCALE_UP")
    _deploy_state(stub, "SUCCESSFUL")
    replace_images(
        client,
        TARGET,
        {"web": NEW_WEB, "was": NEW_WAS},
        100.0,
        desired_count=2,
        clock=lambda: 0.0,
        sleep=sleeps.append,
    )
    assert sleeps == [2.0]


def _rollback(client: Any) -> str | None:
    return rollback_in_flight(client, TARGET, 100.0, clock=lambda: 0.0, sleep=lambda _s: None)


def test_rollback_in_flight_stops_active_deployment_with_rollback(
    client: Any, stub: Stubber
) -> None:
    _list(stub, ACTIVE, [{"serviceDeploymentArn": DEPLOY_ARN, "status": "IN_PROGRESS"}])
    _list(stub, ROLLING_BACK)
    stub.add_response(
        "stop_service_deployment",
        {"serviceDeploymentArn": DEPLOY_ARN},
        {"serviceDeploymentArn": DEPLOY_ARN, "stopType": "ROLLBACK"},
    )
    _deploy_state(stub, "ROLLBACK_IN_PROGRESS")
    _deploy_state(stub, "ROLLBACK_SUCCESSFUL")
    assert _rollback(client) == "진행 중 배포를 ECS 롤백으로 되돌렸다"


def test_rollback_in_flight_does_not_roll_back_twice(client: Any, stub: Stubber) -> None:
    """회로 차단기가 이미 자동 롤백 중이면 stop을 부르지 않고 끝나기만 기다린다."""
    _list(stub, ACTIVE)
    _list(
        stub, ROLLING_BACK, [{"serviceDeploymentArn": DEPLOY_ARN, "status": "ROLLBACK_IN_PROGRESS"}]
    )
    _deploy_state(stub, "ROLLBACK_SUCCESSFUL")
    assert "자동 롤백" in (_rollback(client) or "")


def test_rollback_in_flight_nothing_to_do(client: Any, stub: Stubber) -> None:
    _list(stub, ACTIVE)
    _list(stub, ROLLING_BACK)
    assert _rollback(client) is None


def test_rollback_failure_is_reported(client: Any, stub: Stubber) -> None:
    _list(stub, ACTIVE, [{"serviceDeploymentArn": DEPLOY_ARN, "status": "IN_PROGRESS"}])
    _list(stub, ROLLING_BACK)
    stub.add_response("stop_service_deployment", {"serviceDeploymentArn": DEPLOY_ARN}, None)
    _deploy_state(stub, "ROLLBACK_FAILED")
    with pytest.raises(DdakToolError) as err:
        _rollback(client)
    assert err.value.code is ErrorCode.ADAPTER_FAILED


# 트래픽 전환 시점 완료(10/3 시연 시간 단축): 대상 그룹을 알면 이전 태스크 정리를 기다리지 않는다.
TG_ARN = "arn:aws:elasticloadbalancing:ap-northeast-2:111122223333:targetgroup/flaskr/0123abcd"
TG_TARGET = EcsService(cluster="ddak", service="flaskr", target_group=TG_ARN)
NEW_IPS = ("10.0.1.10", "10.0.2.20")
OLD_IP = "10.0.1.30"


@pytest.fixture
def elb() -> Any:
    return boto3.client(
        "elbv2",
        region_name="ap-northeast-2",
        aws_access_key_id="test" + "ing",
        aws_secret_access_key="test" + "ing",
    )


@pytest.fixture
def elb_stub(elb: Any) -> Iterator[Stubber]:
    with Stubber(elb) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def _rolling(running: int = 2, pending: int = 0, old_desired: int = 0) -> dict[str, Any]:
    new = {**_deployment(NEW_ARN, "PRIMARY", "IN_PROGRESS"), "desiredCount": 2}
    new.update(runningCount=running, pendingCount=pending)
    old = {**_deployment(OLD_ARN, "ACTIVE", "COMPLETED"), "desiredCount": old_desired}
    return _service(NEW_ARN, deployments=[new, old])


def _tasks(stub: Stubber, ips: tuple[str, ...] = NEW_IPS) -> None:
    arns = [f"arn:aws:ecs:ap-northeast-2:111122223333:task/ddak/{i}" for i in range(len(ips) + 1)]
    stub.add_response(
        "list_tasks",
        {"taskArns": arns},
        {"cluster": "ddak", "serviceName": "flaskr", "desiredStatus": "RUNNING"},
    )

    def task(arn: str, definition: str, ip: str, status: str = "RUNNING") -> dict[str, Any]:
        eni = {"name": "privateIPv4Address", "value": ip}
        return {
            "taskArn": arn,
            "taskDefinitionArn": definition,
            "lastStatus": status,
            "attachments": [{"type": "ElasticNetworkInterface", "details": [eni]}],
        }

    tasks = [task(a, NEW_ARN, ip) for a, ip in zip(arns, ips, strict=False)]
    tasks.append(task(arns[-1], OLD_ARN, OLD_IP))  # 정리 중인 이전 태스크(새 IP 집합에 넣지 않음)
    stub.add_response("describe_tasks", {"tasks": tasks}, {"cluster": "ddak", "tasks": arns})


def _health(elb_stub: Stubber, states: dict[str, str]) -> None:
    elb_stub.add_response(
        "describe_target_health",
        {
            "TargetHealthDescriptions": [
                {"Target": {"Id": ip, "Port": 8080}, "TargetHealth": {"State": state}}
                for ip, state in states.items()
            ]
        },
        {"TargetGroupArn": TG_ARN},
    )


def _wait(client: Any, elb: Any) -> None:
    ticks = iter(range(0, 1000, 5))  # clock 호출마다 5초씩 흐른다
    wait_stable(
        client,
        TG_TARGET,
        NEW_ARN,
        1000.0,
        elb=elb,
        clock=lambda: float(next(ticks)),
        sleep=lambda _s: None,
    )


SWITCHED = {NEW_IPS[0]: "healthy", NEW_IPS[1]: "healthy", OLD_IP: "draining"}


def _switched(stub: Stubber, elb_stub: Stubber) -> None:
    _describe(stub, _rolling())
    _tasks(stub)
    _health(elb_stub, SWITCHED)


def test_wait_ends_when_only_new_tasks_take_traffic_for_settle_time(
    client: Any, stub: Stubber, elb: Any, elb_stub: Stubber
) -> None:
    _switched(stub, elb_stub)  # t=0 전환 확인
    _switched(stub, elb_stub)  # t=10 계속 유지 -> 끝(이전 배포가 남아 있어도, COMPLETED 전)
    _wait(client, elb)


def test_wait_restarts_settle_time_when_switch_breaks(
    client: Any, stub: Stubber, elb: Any, elb_stub: Stubber
) -> None:
    _switched(stub, elb_stub)  # t=0
    _describe(stub, _rolling())  # t=10 이전 대상이 다시 healthy(ALB 반영 전) -> 다시 잰다
    _tasks(stub)
    _health(elb_stub, {**SWITCHED, OLD_IP: "healthy"})
    _switched(stub, elb_stub)  # t=20 다시 시작
    _switched(stub, elb_stub)  # t=30 유지 -> 끝
    _wait(client, elb)


@pytest.mark.parametrize("old_state", ["healthy", "initial", "unhealthy"])
def test_wait_continues_while_old_target_can_take_traffic(
    client: Any, stub: Stubber, elb: Any, elb_stub: Stubber, old_state: str
) -> None:
    _describe(stub, _rolling())
    _tasks(stub)
    _health(elb_stub, {NEW_IPS[0]: "healthy", NEW_IPS[1]: "healthy", OLD_IP: old_state})
    _describe(stub, _service(NEW_ARN, deployments=[_deployment(NEW_ARN, "PRIMARY", "COMPLETED")]))
    _wait(client, elb)


def test_wait_continues_until_every_new_target_is_healthy(
    client: Any, stub: Stubber, elb: Any, elb_stub: Stubber
) -> None:
    _describe(stub, _rolling())
    _tasks(stub)
    _health(elb_stub, {NEW_IPS[0]: "healthy", NEW_IPS[1]: "initial", OLD_IP: "draining"})
    _switched(stub, elb_stub)
    _switched(stub, elb_stub)
    _wait(client, elb)


def test_wait_continues_when_new_target_is_not_registered_yet(
    client: Any, stub: Stubber, elb: Any, elb_stub: Stubber
) -> None:
    _describe(stub, _rolling())
    _tasks(stub)
    _health(elb_stub, {NEW_IPS[0]: "healthy", OLD_IP: "draining"})
    _describe(stub, _service(NEW_ARN, deployments=[_deployment(NEW_ARN, "PRIMARY", "COMPLETED")]))
    _wait(client, elb)


@pytest.mark.parametrize(
    "counts",
    [{"running": 1}, {"pending": 1}, {"old_desired": 1}],
    ids=["running", "pending", "old"],
)
def test_wait_skips_target_check_until_ecs_counts_settle(
    client: Any, stub: Stubber, elb: Any, elb_stub: Stubber, counts: dict[str, int]
) -> None:
    _describe(stub, _rolling(**counts))  # 태스크·대상 그룹을 읽지 않는다(스텁에 없음)
    _describe(stub, _service(NEW_ARN, deployments=[_deployment(NEW_ARN, "PRIMARY", "COMPLETED")]))
    _wait(client, elb)


def test_wait_still_fails_on_circuit_breaker_with_target_group(
    client: Any, stub: Stubber, elb: Any
) -> None:
    _describe(stub, _service(NEW_ARN, deployments=[_deployment(NEW_ARN, "PRIMARY", "FAILED")]))
    with pytest.raises(DdakToolError, match="회로 차단기") as exc:
        _wait(client, elb)
    assert exc.value.code is ErrorCode.ADAPTER_FAILED


def test_replace_refuses_when_service_drifted_from_last_release(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN))
    _read_definition(stub)  # 실행 중: OLD_WEB·OLD_WAS
    stub.add_response(
        "register_task_definition", {"taskDefinition": {"taskDefinitionArn": NEW_ARN}}, None
    )
    with pytest.raises(DdakToolError, match="직전 성공 기록과 다른 이미지") as exc:
        _run(  # 기록은 web=NEW_WEB인데 서비스는 OLD_WEB(자동 롤백 등). update_service 전에 멈춘다
            client,
            {"web": NEW_WEB, "was": NEW_WAS},
            expected_current={"web": NEW_WEB, "was": OLD_WAS},
        )
    assert exc.value.code is ErrorCode.PRECONDITION_FAILED


def test_replace_accepts_last_release_or_requested_images(client: Any, stub: Stubber) -> None:
    _describe(stub, _service(OLD_ARN))
    _read_definition(stub, web=NEW_WEB)  # web은 같은 run의 앞 tier 호출이 이미 바꿨다
    stub.add_response(
        "register_task_definition", {"taskDefinition": {"taskDefinitionArn": NEW_ARN}}, None
    )
    _describe(stub, _service(OLD_ARN))
    stub.add_response("update_service", {"service": {}}, None)
    _done(stub)
    got = _run(
        client, {"web": NEW_WEB, "was": NEW_WAS}, expected_current={"web": OLD_WEB, "was": OLD_WAS}
    )
    assert got.changed is True
