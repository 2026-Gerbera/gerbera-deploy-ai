"""ECS 배포 소유권과 저장소 출력 계약. Terraform/AWS 실행 없이 검증한다."""

import json
from dataclasses import replace

import pytest

from ddak.cloud.infra.policy import PolicyViolation, static_gate
from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.infra_outputs import checked_cloud_outputs, checked_outputs
from tests.unit.cloud.infra.test_pipeline_policy import summary
from tests.unit.cloud.infra.test_runtime import SETTINGS, resource


@pytest.mark.parametrize(
    "names,valid",
    [
        (["web", "was"], True),
        (["was"], True),
        (["db"], False),
        (["app"], False),
        (["web", "web"], False),
        ([None], False),
        (["${var.tier}"], False),
    ],
)
def test_task_names_match_tiers_in_hcl_and_plan(names, valid):
    encoded = json.dumps([{"name": name, "image": "fixture"} for name in names])
    source = (
        'resource "aws_ecs_task_definition" "app" { container_definitions = '
        + json.dumps(encoded)
        + " }"
    )
    assert static_gate({"main.tf": source}, layer="platform").passed is valid
    item = resource(
        "aws_ecs_task_definition", "app", ["create"], {"container_definitions": encoded}
    )
    if valid:
        assert summary(item)["counts"]["create"] == 1
    else:
        with pytest.raises(PolicyViolation, match="CONTAINER_NAME"):
            summary(item)


@pytest.mark.parametrize(
    "ignored,valid",
    [
        ("[task_definition, desired_count]", True),
        ("[task_definition, desired_count, tags]", True),
        ('["task_definition", "desired_count"]', True),
        ("[task_definition]", False),
        ("[desired_count]", False),
        ("[]", False),
        ("all", False),
        ('"task_definition,desired_count"', False),
        ("[var.fields]", False),
        (None, False),
    ],
)
def test_service_lifecycle_keeps_code_owned_revision_and_count(ignored, valid):
    lifecycle = "" if ignored is None else f"lifecycle {{ ignore_changes = {ignored} }}"
    source = (
        'resource "aws_ecs_service" "app" {\n'
        + lifecycle
        + "\ndeployment_circuit_breaker { enable = true\n rollback = true }\n}"
    )
    result = static_gate({"main.tf": source}, layer="platform")
    assert result.passed is valid
    if not valid:
        assert result.detail == "ECS_DEPLOYMENT_OWNED_BY_C2"


@pytest.mark.parametrize(
    "breaker",
    [
        None,
        {},
        {"enable": True},
        {"enable": False, "rollback": True},
        {"enable": True, "rollback": False},
        {"enable": "true", "rollback": True},
        {"enable": True, "rollback": True},
    ],
)
def test_circuit_breaker_is_required_in_hcl_and_resolved_plan(breaker):
    valid = breaker == {"enable": True, "rollback": True}
    block = (
        ""
        if breaker is None
        else "deployment_circuit_breaker {\n"
        + "\n".join(key + " = " + json.dumps(value) for key, value in breaker.items())
        + "\n}"
    )
    source = (
        'resource "aws_ecs_service" "app" {\n'
        "lifecycle { ignore_changes = [task_definition, desired_count] }\n" + block + "\n}"
    )
    assert static_gate({"main.tf": source}, layer="platform").passed is valid
    body = {} if breaker is None else {"deployment_circuit_breaker": [breaker]}
    item = resource("aws_ecs_service", "app", ["create"], body)
    if valid:
        assert summary(item)["counts"]["create"] == 1
    else:
        with pytest.raises(PolicyViolation, match="CIRCUIT_BREAKER"):
            summary(item)


@pytest.mark.parametrize(
    "repo,valid",
    [
        ("a/b", True),
        ("team-name/app.web_v2", True),
        ("fixture/app", True),
        ("Team/app", False),
        ("a-/b", False),
        ("a/b_", False),
        ("a/b..c", False),
        ("a/b/c", False),
        ("a/b:tag", False),
        ("a/b\n", False),
    ],
)
def test_repository_literal_and_checked_value_use_same_pattern(repo, valid):
    def setting():
        return replace(
            SETTINGS, layer="platform", outputs={"image_repository": (json.dumps(repo), "string")}
        )

    if valid:
        assert setting().framework()["output"]["image_repository"]["value"] == repo
        assert checked_outputs({"image_repository": repo}, "platform")["image_repository"] == repo
    else:
        with pytest.raises(DdakToolError):
            setting()
        with pytest.raises(ValueError):
            checked_outputs({"image_repository": repo}, "platform")


@pytest.mark.parametrize(
    "key,value",
    [
        ("app_secret_arn_SECRET_KEY", "fixture-plaintext"),
        ("task_execution_role_arn", "fixture-plaintext"),
        ("target_group_arn", "fixture-plaintext"),
        ("ecs_service_name", []),
        ("region", "ap-northeast-2"),
        ("ecs_container_name_was", "was"),
        ("SECRET_KEY", "fixture-plaintext"),
    ],
)
def test_unlisted_or_non_identifier_outputs_are_not_persistable(key, value):
    with pytest.raises(ValueError):
        checked_cloud_outputs({key: value})
