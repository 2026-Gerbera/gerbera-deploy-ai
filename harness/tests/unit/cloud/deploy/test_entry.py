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
DB_ARN = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:ddak/flaskr/DB-AbCdEf"
RDS_ARN = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:rds!db-1-AbCdEf"
DOMAIN = "app.example.test"
SHA = "c" * 40
PLATFORM = {
    "cloud": {
        "cluster_name": "ddak",
        "ecs_service_name": "flaskr",
        "public_subnet_ids": ["subnet-1"],
        "app_security_group_id": "sg-1",
        "app_secret_arn_SECRET_KEY": SECRET_ARN,
        "app_secret_arn_DATABASE_URL": DB_ARN,
        "rds_endpoint": "db.example.internal:3306",
        "rds_master_secret_arn": RDS_ARN,
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
WEB_AMD64 = "sha256:" + "c" * 64
WEB_ARM64 = "sha256:" + "d" * 64
CARRIED_WEB = {  # 이번 run에서 빌드하지 않은 web(후속 수정 7 CarriedImageSource)
    "artifact": {
        "ref": NEW_WEB,
        "index_digest": "sha256:" + "7" * 64,
        "platform_digests": {"linux/amd64": WEB_AMD64, "linux/arm64": WEB_ARM64},
    },
    "release_id": "run-0",
    "carried_forward": True,
}
PREVIOUS = {
    "cloud": {
        "release_id": "run-0",
        "images": {"web": NEW_WEB, "was": OLD_WAS},
        "image_sources": {"web": CARRIED_WEB},
    }
}
SVC = {"cluster": "ddak", "services": ["flaskr"]}


def _ctx(**kw: Any) -> RunContext:
    base: dict[str, Any] = {
        "adapter_mode": AdapterMode.REAL,
        "platform": PLATFORM,
        "images": {"web": NEW_WEB, "was": NEW_WAS, "db": "mysql@sha256:" + "6" * 64},
        "release_artifacts": ARTIFACTS,
        "deadline": 10**9,
        "cloud_domain": DOMAIN,
        "source_sha": SHA,
        "project": "flaskr",
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


def _containers(web: str, was: str, release_id: str | None = None) -> list[dict[str, Any]]:
    """release_id를 주면 그 릴리스의 런타임 설정(앱 env.example·nginx 템플릿)이 들어간 상태."""
    web_c: dict[str, Any] = {"name": "web", "image": web}
    was_c: dict[str, Any] = {"name": "was", "image": was}
    if release_id is not None:
        was_env = {
            "APP_BASE_URL": f"https://{DOMAIN}",
            "APP_ENV": "production",
            "MIGRATE_MODE": "up",
            "PROXY_FIX_X_FOR": "1",
            "PROXY_FIX_X_PROTO": "1",
            "SESSION_COOKIE_SECURE": "true",
            "RELEASE_ID": release_id,
            "SOURCE_SHA": SHA,
        }
        web_c["environment"] = [{"name": "WAS_UPSTREAM", "value": "127.0.0.1:8000"}]
        was_c["environment"] = [{"name": k, "value": v} for k, v in sorted(was_env.items())]
        was_c["secrets"] = [
            {"name": "DATABASE_URL", "valueFrom": DB_ARN},
            {"name": "SECRET_KEY", "valueFrom": SECRET_ARN},
        ]
    return [web_c, was_c]


def _definition(web: str, was: str, release_id: str | None = None) -> dict[str, Any]:
    return {
        "taskDefinition": {
            "family": "flaskr",
            "networkMode": "awsvpc",
            "requiresCompatibilities": ["FARGATE"],
            "cpu": "256",
            "memory": "512",
            "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
            "containerDefinitions": _containers(web, was, release_id),
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
    expected = {
        k: v
        for k, v in _definition(NEW_WEB, NEW_WAS, "run-1")["taskDefinition"].items()
        if k != "taskDefinitionArn"
    }
    ecs.add_response(
        "register_task_definition", {"taskDefinition": {"taskDefinitionArn": NEW_ARN}}, expected
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


def test_second_tier_step_observes_carried_tier(clients: dict[str, Stubber]) -> None:
    ecs = clients["ecs"]
    ecs.add_response("describe_services", _service(NEW_ARN), SVC)
    ecs.add_response("describe_task_definition", _definition(NEW_WEB, NEW_WAS, "run-1"), None)
    ecs.add_response("describe_services", _service(NEW_ARN), SVC)
    ecs.add_response(
        "describe_task_definition",
        _definition(NEW_WEB, NEW_WAS, "run-1"),
        {"taskDefinition": NEW_ARN},
    )
    ecs.add_response("list_tasks", {"taskArns": ["t1"]}, None)
    task = {
        "taskDefinitionArn": NEW_ARN,
        "containers": [{"name": "web", "imageDigest": WEB_ARM64}],
    }
    ecs.add_response("describe_tasks", {"tasks": [task]}, None)
    got = AwsProvider().deploy("web", _ctx(previous_release=PREVIOUS))
    assert got.changed is False  # 같은 run의 첫 step이 이미 전환했다
    # 이번 빌드에 web이 없어 이월 산출물로 관측한다
    assert got.observation is not None
    assert got.observation.platform_digest == WEB_ARM64


def test_deploy_without_build_or_carried_artifact_fails() -> None:
    with pytest.raises(DdakToolError) as err:
        AwsProvider().deploy("web", _ctx())
    assert err.value.code is ErrorCode.PRECONDITION_FAILED


def test_deploy_rejects_carried_ref_mismatch() -> None:
    ref = "docker.io/other/flaskr@sha256:" + "7" * 64  # digest는 같고 저장소가 다르다
    other = {**CARRIED_WEB, "artifact": {**CARRIED_WEB["artifact"], "ref": ref}}
    previous = {"cloud": {**PREVIOUS["cloud"], "image_sources": {"web": other}}}
    with pytest.raises(DdakToolError) as err:
        AwsProvider().deploy("web", _ctx(previous_release=previous))
    assert err.value.code is ErrorCode.CONFIG_INVALID


def test_deploy_rejects_tier_without_container() -> None:
    with pytest.raises(DdakToolError) as err:
        AwsProvider().deploy("db", _ctx())
    assert err.value.code is ErrorCode.CONFIG_INVALID


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


ROLLBACK_TO = {
    "cloud": {"release_id": "run-0", "source_sha": SHA, "images": {"web": OLD_WEB, "was": OLD_WAS}}
}


def _no_deployments(ecs: Stubber) -> None:
    """rollback_in_flight: 진행 중·롤백 중 배포가 없다."""
    for statuses in (["PENDING", "IN_PROGRESS"], ["ROLLBACK_REQUESTED", "ROLLBACK_IN_PROGRESS"]):
        ecs.add_response(
            "list_service_deployments",
            {"serviceDeployments": []},
            {"cluster": "ddak", "service": "flaskr", "status": statuses},
        )


def test_rollback_already_on_previous_release_does_nothing(clients: dict[str, Stubber]) -> None:
    ecs = clients["ecs"]
    _no_deployments(ecs)
    ecs.add_response("describe_services", _service(OLD_ARN), SVC)
    ecs.add_response("describe_task_definition", _definition(OLD_WEB, OLD_WAS, "run-0"), None)
    ecs.add_response("describe_services", _service(OLD_ARN), SVC)  # 이미 이전 이미지·RELEASE_ID
    got = AwsProvider().rollback("was", _ctx(previous_release=ROLLBACK_TO))
    assert got.function == "rollback"
    assert got.image_ref == OLD_WAS
    assert got.changed is False


def test_rollback_restores_previous_images_and_release_id(clients: dict[str, Stubber]) -> None:
    ecs = clients["ecs"]
    _no_deployments(ecs)
    ecs.add_response("describe_services", _service(NEW_ARN), SVC)
    ecs.add_response("describe_task_definition", _definition(NEW_WEB, NEW_WAS, "run-1"), None)
    expected = {
        k: v
        for k, v in _definition(OLD_WEB, OLD_WAS, "run-0")["taskDefinition"].items()
        if k != "taskDefinitionArn"
    }
    ecs.add_response(
        "register_task_definition", {"taskDefinition": {"taskDefinitionArn": OLD_ARN}}, expected
    )
    ecs.add_response("describe_services", _service(NEW_ARN), SVC)
    ecs.add_response("update_service", {"service": {}}, None)
    done = [{"taskDefinition": OLD_ARN, "status": "PRIMARY", "rolloutState": "COMPLETED"}]
    ecs.add_response("describe_services", _service(OLD_ARN, deployments=done), SVC)
    got = AwsProvider().rollback("was", _ctx(previous_release=ROLLBACK_TO))
    assert got.changed is True
    assert got.image_ref == OLD_WAS


def test_deploy_needs_cloud_domain() -> None:
    with pytest.raises(DdakToolError) as err:
        AwsProvider().deploy("was", _ctx(cloud_domain=None))
    assert err.value.code is ErrorCode.CONFIG_INVALID


def test_deploy_needs_database_url_secret_output(clients: dict[str, Stubber]) -> None:
    cloud = {k: v for k, v in PLATFORM["cloud"].items() if k != "app_secret_arn_DATABASE_URL"}
    with pytest.raises(DdakToolError) as err:
        AwsProvider().deploy("was", _ctx(platform={"cloud": cloud}))
    assert err.value.code is ErrorCode.INFRA_MISSING


def test_rollback_first_deploy_scales_to_zero(clients: dict[str, Stubber]) -> None:
    ecs = clients["ecs"]
    _no_deployments(ecs)
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


@pytest.mark.parametrize(
    "previous",
    [
        {"cloud": {"images": {}}},
        {"cloud": {"images": {"web": OLD_WEB, "was": OLD_WAS}}},  # release_id 없음
    ],
)
def test_rollback_rejects_malformed_previous_release(previous: dict[str, Any]) -> None:
    with pytest.raises(DdakToolError) as err:
        AwsProvider().rollback("was", _ctx(previous_release=previous))
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


def _empty_secret(sm: Stubber, arn: str) -> None:
    sm.add_response("describe_secret", {"Name": "x", "VersionIdsToStages": {}}, {"SecretId": arn})


def test_inject_config_builds_database_url_from_rds(clients: dict[str, Stubber]) -> None:
    sm = clients["secretsmanager"]
    master = '{"username": "admin", "password": "p@ss/word"}'  # URL 인코딩이 필요한 값
    sm.add_response("get_secret_value", {"SecretString": master}, {"SecretId": RDS_ARN})
    _empty_secret(sm, DB_ARN)
    sm.add_response(  # SECRET_KEY는 요청에 없어도 함께 확인한다(이미 있으면 재사용)
        "describe_secret",
        {"Name": "SECRET_KEY", "VersionIdsToStages": {"0" * 32: ["AWSCURRENT"]}},
        {"SecretId": SECRET_ARN},
    )
    sm.add_response("get_secret_value", {"SecretString": "b" * 64}, {"SecretId": SECRET_ARN})
    sm.add_response(
        "put_secret_value",
        {},
        {
            "SecretId": DB_ARN,
            "SecretString": "mysql+pymysql://admin:p%40ss%2Fword@db.example.internal:3306/flaskr"
            "?charset=utf8mb4&ssl_ca=/app/certs/global-bundle.pem",
        },
    )
    got = AwsProvider().inject_config(["DATABASE_URL"], _ctx())
    assert got.changed is True
    assert "DATABASE_URL" in got.detail
    assert "p@ss" not in got.model_dump_json()


def test_inject_config_skips_public_runtime_keys(clients: dict[str, Stubber]) -> None:
    got = AwsProvider().inject_config(["APP_BASE_URL", "APP_ENV", "RELEASE_ID"], _ctx())
    assert got.changed is False
    assert got.keys == ["APP_BASE_URL", "APP_ENV", "RELEASE_ID"]


def test_inject_config_unknown_key_without_secret_output_fails(clients: dict[str, Stubber]) -> None:
    with pytest.raises(DdakToolError) as err:
        AwsProvider().inject_config(["API_TOKEN"], _ctx())
    assert err.value.code is ErrorCode.INFRA_MISSING


@pytest.mark.parametrize("key", ["DB_MIGRATOR", "db_migrator"])
def test_inject_config_rejects_migration_keys(key: str) -> None:
    with pytest.raises(DdakToolError) as err:
        AwsProvider().inject_config(["SECRET_KEY", key], _ctx())
    assert err.value.code is ErrorCode.CONFIG_INVALID


def test_migrate_db_runs_on_new_was_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _Rev:
        task_definition = NEW_ARN

    def fake_register(ecs: Any, target: Any, images: dict[str, str], **kw: Any) -> _Rev:
        seen["images"] = images
        seen.update(kw)
        return _Rev()

    def fake_phases(ecs: Any, logs: Any, task: Any, migrations: Any, deadline: float) -> dict:
        seen["task"] = task
        return {"event": "MIGRATE_RESULT", "phases": [{"phase": "up", "applied": ["0002"]}]}

    monkeypatch.setattr(_aws, "client", lambda service, ctx: object())
    monkeypatch.setattr(entry_module, "register_revision", fake_register)
    monkeypatch.setattr(entry_module, "run_migration_phases", fake_phases)
    got = AwsProvider().migrate_db(["0002"], _ctx())
    assert seen["images"] == {"was": NEW_WAS}
    assert seen["environment"]["was"]["RELEASE_ID"] == "run-1"
    assert seen["environment"]["web"] == {"WAS_UPSTREAM": "127.0.0.1:8000"}
    assert seen["secrets"] == {"was": {"SECRET_KEY": SECRET_ARN, "DATABASE_URL": DB_ARN}}
    assert seen["only_containers"] == frozenset({"was"})
    assert seen["task"].task_definition == NEW_ARN
    assert seen["task"].container == "was"
    assert list(seen["task"].network.security_groups) == ["sg-1"]
    assert got.changed is True
    assert got.migration is not None


class _StatefulEcs:
    """서비스 상태를 기억하는 가짜 ECS(블루그린). update_service 호출 수를 센다."""

    def __init__(self) -> None:
        self.revisions = {OLD_ARN: _definition(OLD_WEB, OLD_WAS)["taskDefinition"]}
        self.current = OLD_ARN
        self.desired = 0
        self.updates = 0

    def describe_services(self, **_: Any) -> dict[str, Any]:
        return {
            "services": [
                {
                    "status": "ACTIVE",
                    "taskDefinition": self.current,
                    "desiredCount": self.desired,
                    "deploymentConfiguration": {"strategy": "BLUE_GREEN"},
                }
            ]
        }

    def describe_task_definition(self, taskDefinition: str, **_: Any) -> dict[str, Any]:
        return {"taskDefinition": self.revisions[taskDefinition]}

    def register_task_definition(self, **kw: Any) -> dict[str, Any]:
        arn = f"{OLD_ARN.rsplit(':', 1)[0]}:{len(self.revisions) + 4}"
        self.revisions[arn] = {**kw, "taskDefinitionArn": arn}
        return {"taskDefinition": {"taskDefinitionArn": arn}}

    def update_service(self, taskDefinition: str, desiredCount: int | None = None, **_: Any):
        self.updates += 1
        self.current = taskDefinition
        self.desired = desiredCount if desiredCount is not None else self.desired
        return {"service": {"currentServiceDeployment": "arn:deploy"}}

    def list_service_deployments(self, **_: Any) -> dict[str, Any]:
        return {"serviceDeployments": []}

    def describe_service_deployments(self, **_: Any) -> dict[str, Any]:
        return {"serviceDeployments": [{"status": "IN_PROGRESS", "lifecycleStage": "BAKE_TIME"}]}

    def list_tasks(self, **_: Any) -> dict[str, Any]:
        return {"taskArns": ["t1"]}

    def describe_tasks(self, **_: Any) -> dict[str, Any]:
        return {
            "tasks": [
                {
                    "taskDefinitionArn": self.current,
                    "containers": [
                        {"name": "web", "imageDigest": WEB_ARM64},
                        {"name": "was", "imageDigest": ARM64},
                    ],
                }
            ]
        }


def test_one_update_service_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """deploy.web·deploy.was 두 step이 돌아도 서비스 전환(블루그린 green 기동)은 한 번이다."""
    ecs = _StatefulEcs()
    monkeypatch.setattr(_aws, "client", lambda service, ctx: ecs)
    web = ImageArtifact(**CARRIED_WEB["artifact"])
    artifacts = ARTIFACTS.model_copy(update={"images": {**ARTIFACTS.images, "web": web}})
    ctx = _ctx(release_artifacts=artifacts)
    first = AwsProvider().deploy("web", ctx)
    second = AwsProvider().deploy("was", ctx)
    assert (first.changed, second.changed) == (True, False)
    assert ecs.updates == 1
    assert ecs.desired == 2  # 첫 배포는 0 → 2
