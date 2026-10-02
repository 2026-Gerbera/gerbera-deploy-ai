"""AwsProvider 진입점: ctx 값으로 ECS·시크릿·마이그레이션을 부르는 연결(R9 (a)). 네트워크 없음."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber

from ddak.cloud.deploy import AwsProvider, _aws
from ddak.cloud.deploy import entry as entry_module
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.release import ImageArtifact, ReleaseArtifacts, SnapshotBinding

INDEX = "sha256:" + "1" * 64
AMD64 = "sha256:" + "2" * 64
ARM64 = "sha256:" + "3" * 64
OLD_WEB = "docker.io/gerbera/flaskr@sha256:" + "8" * 64
OLD_WAS = "docker.io/gerbera/flaskr@sha256:" + "9" * 64
NEW_WEB = "docker.io/gerbera/flaskr@sha256:" + "7" * 64
NEW_WAS = "docker.io/gerbera/flaskr@" + INDEX
OLD_ARN = "arn:aws:ecs:ap-northeast-2:111122223333:task-definition/flaskr:4"
NEW_ARN = "arn:aws:ecs:ap-northeast-2:111122223333:task-definition/flaskr:5"
SECRET_ARN = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:ddak/flaskr/SK-AbCdEf"
PLATFORM = {
    "cloud": {
        "cluster_name": "ddak",
        "ecs_service_name": "flaskr",
        "ecs_container_name_web": "web",
        "ecs_container_name_was": "was",
        "public_subnet_ids": ["subnet-1"],
        "app_security_group_id": "sg-1",
        "app_secret_arn_SECRET_KEY": SECRET_ARN,
    }
}
ARTIFACTS = ReleaseArtifacts(
    snapshot=SnapshotBinding(
        source_snapshot_hash="sha256:" + "a" * 64, build_snapshot_hash="sha256:" + "a" * 64
    ),
    images={
        "was": ImageArtifact(
            ref=NEW_WAS,
            index_digest=INDEX,
            platform_digests={"linux/amd64": AMD64, "linux/arm64": ARM64},
        )
    },
)
SVC = {"cluster": "ddak", "services": ["flaskr"]}


def _ctx(**kw: Any) -> RunContext:
    base: dict[str, Any] = {
        "adapter_mode": AdapterMode.REAL,
        "platform": PLATFORM,
        "images": {"web": NEW_WEB, "was": NEW_WAS, "db": "mysql@sha256:" + "6" * 64},
        "release_artifacts": ARTIFACTS,
        "deadline": 10**9,
    }
    base.update(kw)
    return RunContext("run-1", **base)


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Stubber]]:
    made = {
        name: boto3.client(
            name,
            region_name="ap-northeast-2",
            aws_access_key_id="test" + "ing",
            aws_secret_access_key="test" + "ing",
        )
        for name in ("ecs", "secretsmanager", "logs")
    }
    monkeypatch.setattr(_aws, "client", lambda service, ctx: made[service])
    stubs = {name: Stubber(c) for name, c in made.items()}
    for s in stubs.values():
        s.activate()
    yield stubs
    for s in stubs.values():
        s.assert_no_pending_responses()
        s.deactivate()


def _service(arn: str, desired: int = 2, **kw: Any) -> dict[str, Any]:
    svc = {
        "serviceName": "flaskr",
        "status": "ACTIVE",
        "taskDefinition": arn,
        "desiredCount": desired,
        **kw,
    }
    return {"services": [svc]}


def _definition(web: str, was: str) -> dict[str, Any]:
    return {
        "taskDefinition": {
            "family": "flaskr",
            "networkMode": "awsvpc",
            "requiresCompatibilities": ["FARGATE"],
            "cpu": "256",
            "memory": "512",
            "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
            "containerDefinitions": [{"name": "web", "image": web}, {"name": "was", "image": was}],
        }
    }


def _observe(ecs: Stubber, running_digest: str) -> None:
    ecs.add_response(
        "describe_task_definition", _definition(NEW_WEB, NEW_WAS), {"taskDefinition": NEW_ARN}
    )
    ecs.add_response(
        "list_tasks",
        {"taskArns": ["arn:aws:ecs:ap-northeast-2:111122223333:task/ddak/t1"]},
        {"cluster": "ddak", "serviceName": "flaskr", "desiredStatus": "RUNNING"},
    )
    task = {
        "taskDefinitionArn": NEW_ARN,
        "containers": [
            {"name": "web", "imageDigest": "sha256:" + "5" * 64},
            {"name": "was", "imageDigest": running_digest},
        ],
    }
    ecs.add_response("describe_tasks", {"tasks": [task]}, {"cluster": "ddak", "tasks": ANY})


def _first_tier_switch(ecs: Stubber, running_digest: str) -> None:
    ecs.add_response("describe_services", _service(OLD_ARN), SVC)
    ecs.add_response(
        "describe_task_definition",
        _definition(OLD_WEB, OLD_WAS),
        {"taskDefinition": OLD_ARN, "include": ["TAGS"]},
    )
    ecs.add_response(
        "register_task_definition", {"taskDefinition": {"taskDefinitionArn": NEW_ARN}}, None
    )
    ecs.add_response("describe_services", _service(OLD_ARN), SVC)
    ecs.add_response("update_service", {"service": {}}, None)
    done = [{"taskDefinition": NEW_ARN, "status": "PRIMARY", "rolloutState": "COMPLETED"}]
    ecs.add_response("describe_services", _service(NEW_ARN, deployments=done), SVC)
    _observe(ecs, running_digest)


def test_provider_module_path() -> None:
    assert AwsProvider.__module__ == "ddak.cloud.deploy.providers.aws"


@pytest.mark.parametrize("running", [ARM64, INDEX])
def test_deploy_switches_all_containers_and_observes_tier(
    clients: dict[str, Stubber], running: str
) -> None:
    _first_tier_switch(clients["ecs"], running)
    got = AwsProvider().deploy("was", _ctx())
    assert got.function == "deploy"
    assert got.changed is True
    assert got.image_ref == NEW_WAS
    assert got.previous_image == OLD_WAS
    assert got.observation is not None
    assert got.observation.platform == "linux/arm64"
    assert got.observation.platform_digest == ARM64


def test_second_tier_step_does_not_redeploy(clients: dict[str, Stubber]) -> None:
    ecs = clients["ecs"]
    ecs.add_response("describe_services", _service(NEW_ARN), SVC)
    ecs.add_response("describe_task_definition", _definition(NEW_WEB, NEW_WAS), None)
    ecs.add_response("describe_services", _service(NEW_ARN), SVC)
    ecs.add_response(
        "describe_task_definition", _definition(NEW_WEB, NEW_WAS), {"taskDefinition": NEW_ARN}
    )
    ecs.add_response("list_tasks", {"taskArns": ["t1"]}, None)
    task = {"taskDefinitionArn": NEW_ARN, "containers": [{"name": "web", "imageDigest": AMD64}]}
    ecs.add_response("describe_tasks", {"tasks": [task]}, None)
    got = AwsProvider().deploy("web", _ctx())
    assert got.changed is False
    assert got.observation is None  # 이번 빌드 산출물에 web이 없다(테스트 ARTIFACTS)


def test_deploy_rejects_foreign_running_digest(clients: dict[str, Stubber]) -> None:
    _first_tier_switch(clients["ecs"], "sha256:" + "f" * 64)
    with pytest.raises(DdakToolError) as err:
        AwsProvider().deploy("was", _ctx())
    assert err.value.code is ErrorCode.ADAPTER_FAILED


def test_deploy_needs_infra_outputs() -> None:
    with pytest.raises(DdakToolError) as err:
        AwsProvider().deploy("was", _ctx(platform={"cloud": {"cluster_name": "ddak"}}))
    assert err.value.code is ErrorCode.INFRA_MISSING


def test_fake_context_never_calls_aws() -> None:
    with pytest.raises(DdakToolError) as err:
        AwsProvider().deploy("was", _ctx(adapter_mode=AdapterMode.FAKE))
    assert err.value.code is ErrorCode.CONFIG_INVALID


def test_rollback_restores_previous_images(clients: dict[str, Stubber]) -> None:
    ecs = clients["ecs"]
    ecs.add_response("describe_services", _service(OLD_ARN), SVC)
    ecs.add_response("describe_task_definition", _definition(OLD_WEB, OLD_WAS), None)
    ecs.add_response("describe_services", _service(OLD_ARN), SVC)  # 이미 이전 이미지
    previous = {"cloud": {"release_id": "run-0", "images": {"web": OLD_WEB, "was": OLD_WAS}}}
    got = AwsProvider().rollback("was", _ctx(previous_release=previous))
    assert got.function == "rollback"
    assert got.image_ref == OLD_WAS
    assert got.changed is False


def test_rollback_first_deploy_scales_to_zero(clients: dict[str, Stubber]) -> None:
    ecs = clients["ecs"]
    ecs.add_response("describe_services", _service(NEW_ARN), SVC)
    ecs.add_response(
        "update_service",
        {"service": {}},
        {"cluster": "ddak", "service": "flaskr", "desiredCount": 0},
    )
    ecs.add_response("describe_services", _service(NEW_ARN, desired=0, runningCount=0), SVC)
    got = AwsProvider().rollback("was", _ctx())
    assert got.changed is True
    assert got.image_ref is None


def test_rollback_rejects_malformed_previous_release() -> None:
    with pytest.raises(DdakToolError) as err:
        AwsProvider().rollback("was", _ctx(previous_release={"cloud": {"images": {}}}))
    assert err.value.code is ErrorCode.CONFIG_INVALID


def test_inject_config_uses_secret_outputs(clients: dict[str, Stubber]) -> None:
    clients["secretsmanager"].add_response(
        "describe_secret",
        {"Name": "SECRET_KEY", "VersionIdsToStages": {"0" * 32: ["AWSCURRENT"]}},
        {"SecretId": SECRET_ARN},
    )
    clients["secretsmanager"].add_response(
        "get_secret_value", {"SecretString": "b" * 64}, {"SecretId": SECRET_ARN}
    )
    got = AwsProvider().inject_config(["SECRET_KEY"], _ctx())
    assert got.function == "inject_config"
    assert got.keys == ["SECRET_KEY"]
    assert got.changed is False


def test_migrate_db_runs_on_new_was_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _Rev:
        task_definition = NEW_ARN

    def fake_register(ecs: Any, target: Any, images: dict[str, str]) -> _Rev:
        seen["images"] = images
        return _Rev()

    def fake_phases(ecs: Any, logs: Any, task: Any, migrations: Any, deadline: float) -> dict:
        seen["task"] = task
        return {"event": "MIGRATE_RESULT", "phases": [{"phase": "up", "applied": ["0002"]}]}

    monkeypatch.setattr(_aws, "client", lambda service, ctx: object())
    monkeypatch.setattr(entry_module, "register_revision", fake_register)
    monkeypatch.setattr(entry_module, "run_migration_phases", fake_phases)
    got = AwsProvider().migrate_db(["0002"], _ctx())
    assert seen["images"] == {"was": NEW_WAS}
    assert seen["task"].task_definition == NEW_ARN
    assert seen["task"].container == "was"
    assert list(seen["task"].network.security_groups) == ["sg-1"]
    assert got.changed is True
    assert got.migration is not None
