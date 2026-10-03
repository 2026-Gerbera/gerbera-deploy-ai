"""ECS 프로젝트 로그 범위와 CodeBuild 호환성. 실제 SDK/CLI 호출 없음."""

import json

import pytest

from ddak.cloud.infra.policy import PolicyViolation, inspect_policy, static_gate
from ddak.cloud.infra.providers.aws import build_boundary_document
from tests.unit.cloud.infra import test_runtime as f
from tests.unit.cloud.infra.test_bootstrap_dbinit_policy import summarize

# {project}/{account}는 HCL에서는 변수, plan에서는 확정값으로 치환한다.
PREFIX = "arn:aws:logs:ap-northeast-2:{account}:log-group:"
CASES = [
    pytest.param(["/aws/ecs/{project}"], True, id="project-exact"),
    pytest.param(["/aws/ecs/{project}:*"], True, id="project-streams"),
    pytest.param(["/aws/ecs/{project}*"], True, id="project-prefix"),
    pytest.param(["/aws/ecs/ddak-*:*"], True, id="legacy-ddak"),
    pytest.param(["/aws/ecs/ddak-{project}-was:*"], True, id="legacy-ddak-project"),
    pytest.param(["/aws/codebuild/ddak-{project}-build:*"], True, id="codebuild-project"),
    pytest.param(["/aws/ecs/ddak-existing_logs:*"], True, id="legacy-ddak-underscore"),
    pytest.param(["/aws/ecs/other-project:*"], False, id="other-project"),
    pytest.param(["/aws/ecs/*"], False, id="all-ecs"),
    pytest.param(["/aws/ecs/{project}:*", "/aws/ecs/ddak-*:*"], True, id="allowed-array"),
    pytest.param(["/aws/ecs/{project}:*", "/aws/ecs/*"], False, id="mixed-array"),
    pytest.param(["/aws/ecs/*", "/aws/ecs/{project}:*"], False, id="mixed-array-reversed"),
    pytest.param(["/aws/ecs/{project}**"], False, id="double-wildcard"),
    pytest.param(["/aws/ecs/{project}:**"], False, id="stream-double-wildcard"),
    pytest.param(["/aws/ecs/{project}*:other"], False, id="nonterminal-wildcard"),
    pytest.param(["/aws/ecs/{project}?"], False, id="question-wildcard"),
    pytest.param(["/aws/codebuild/ddak-*:*"], True, id="codebuild-preserved"),
]


def policy_for(refs):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": refs,
            }
        ],
    }


def assert_hcl_scope(refs, allowed):
    source = (
        'resource "aws_iam_role_policy" "logs" {\n'
        "  role = aws_iam_role.exec.id\n"
        "  policy = jsonencode(" + json.dumps(policy_for(refs)) + ")\n}"
    )
    result = static_gate({"main.tf": source}, layer="platform")
    assert result.passed is allowed, result.detail


def assert_plan_scope(refs, allowed, action, project):
    raw = f.plan_json()
    # 기존 역할 fixture를 유지하고 secret 리소스는 이 로그 검사에서 제외한다.
    role, _, policy = raw["resource_changes"]
    raw["resource_changes"] = [role, policy]
    policy["change"]["actions"] = [action]
    policy["change"]["after"]["policy"] = json.dumps(policy_for(refs))
    if allowed:
        result = summarize(raw, project=project, exit_code=0 if action == "no-op" else 2)
        assert result["counts"]["create"] == (1 if action == "create" else 0)
    else:
        with pytest.raises(PolicyViolation):
            summarize(raw, project=project, exit_code=0 if action == "no-op" else 2)


@pytest.mark.parametrize("paths,allowed", CASES)
def test_hcl_log_scope_uses_project_variable(paths, allowed):
    refs = [
        (PREFIX + path).format(account="${var.account_id}", project="${var.project}")
        for path in paths
    ]
    assert_hcl_scope(refs, allowed)


@pytest.mark.parametrize("paths,allowed", CASES)
def test_shared_policy_inspector_enforces_hcl_log_scope(paths, allowed):
    refs = [
        (PREFIX + path).format(account="${var.account_id}", project="${var.project}")
        for path in paths
    ]
    if allowed:
        inspect_policy(policy_for(refs))
    else:
        with pytest.raises(PolicyViolation):
            inspect_policy(policy_for(refs))


@pytest.mark.parametrize("paths,allowed", CASES)
@pytest.mark.parametrize("action", ["create", "no-op"])
@pytest.mark.parametrize("project", ["flaskr", "inventory-api"])
def test_resolved_plan_log_scope_including_noop(paths, allowed, action, project):
    refs = [(PREFIX + path).format(account=f.ACCOUNT, project=project) for path in paths]
    assert_plan_scope(refs, allowed, action, project)


@pytest.mark.parametrize("scalar", [False, True])
@pytest.mark.parametrize("wrong", ["account", "region", "account-wildcard", "region-wildcard"])
@pytest.mark.parametrize("stage", ["hcl", "create", "no-op"])
def test_log_scope_rejects_wrong_account_or_region(scalar, wrong, stage):
    account = "${var.account_id}" if stage == "hcl" else f.ACCOUNT
    project = "${var.project}" if stage == "hcl" else f.SETTINGS.project
    region = "ap-northeast-2"
    if wrong == "account":
        account = "999999999999"
    elif wrong == "account-wildcard":
        account = "*"
    elif wrong == "region":
        region = "us-east-1"
    else:
        region = "*"
    arn = f"arn:aws:logs:{region}:{account}:log-group:/aws/ecs/{project}:*"
    refs = arn if scalar else [arn]
    if stage == "hcl":
        assert_hcl_scope(refs, False)
    else:
        assert_plan_scope(refs, False, stage, project)


def test_codebuild_boundary_keeps_existing_log_scope():
    logs = [
        row
        for row in build_boundary_document(f.ACCOUNT)["Statement"]
        if "logs:PutLogEvents" in row["Action"]
    ]
    assert logs == [
        {
            "Effect": "Allow",
            "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": (
                f"arn:aws:logs:ap-northeast-2:{f.ACCOUNT}:log-group:/aws/codebuild/ddak-*:*"
            ),
        }
    ]
