"""C1 실행부: 네트워크 없는 fake Terraform/AWS 경계 검증."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from ddak.cloud.infra.foundation import (
    apply_foundation,
    foundation_approval_hash,
    foundation_template,
)
from ddak.cloud.infra.plan import filter_outputs, summarize_plan
from ddak.cloud.infra.policy import OVERRIDE_REQUIRED_BUILDSPEC, PolicyViolation, static_gate
from ddak.cloud.infra.runtime import (
    AwsSettings,
    CommandResult,
    CommandRunner,
    InfraRuntime,
    SessionKeys,
    canonical,
    digest,
)
from ddak.core.contracts.approval import ApprovalRecord
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.executor.approval_meta import encode_meta

ACCOUNT = "123456789012"
SETTINGS = AwsSettings(
    "flaskr",
    ACCOUNT,
    "ddak-fixture-state",
    "app",
    {"secret_arn": ("aws_secretsmanager_secret.session.arn", "string")},
)
SECRET = f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:ddak/flaskr/SECRET_KEY-??????"
HCL = """resource "aws_secretsmanager_secret" "session" {
  name = "ddak/${var.project}/SECRET_KEY"
}
"""
SESSION = SessionKeys("fixture-access", "fixture-" + "secret", "fixture-session")


def test_foundation_iam_client_disables_sdk_retries(tmp_path, monkeypatch):
    sdk = Mock()
    monkeypatch.setattr("ddak.cloud.infra.runtime.boto3.Session", Mock(return_value=sdk))
    runtime = InfraRuntime(
        root=tmp_path,
        run_id="retry-fixture",
        settings=SETTINGS,
        lock_file=b"fixture",
        approvals=lambda: [],
        guard=Mock(),
        runner=FakeRunner(),
    )
    runtime._clients(SESSION)
    configs = {call.args[0]: call.kwargs["config"] for call in sdk.client.call_args_list}
    assert configs["iam"].retries == {"total_max_attempts": 1}
    assert configs["s3"].retries == configs["sts"].retries == {"max_attempts": 2}


def approval(sha, kind="infra", **changes):
    data = dict(
        run_id="run-1",
        project="flaskr",
        approval_id="approval-1",
        kind=kind,
        bound_to=sha,
        approver="human",
        approved_at=datetime.now(UTC),
        decision="approved",
    )
    data.update(changes)
    return ApprovalRecord(**data)


def resource(kind, name, actions, after, **extra):
    return dict(
        type=kind,
        mode="managed",
        address=f"{kind}.{name}",
        change=dict(actions=actions, after=after, after_unknown={}, after_sensitive={}, **extra),
    )


def plan_json():
    role = dict(
        name="ddak-flaskr-exec", path="/ddak/app/", permissions_boundary=SETTINGS.boundary_arn
    )
    policy = dict(
        Version="2012-10-17",
        Statement=[
            dict(Effect="Allow", Action=["secretsmanager:GetSecretValue"], Resource=[SECRET])
        ],
    )
    return dict(
        format_version="1.2",
        resource_changes=[
            resource("aws_iam_role", "exec", ["no-op"], role),
            resource(
                "aws_secretsmanager_secret",
                "session",
                ["create"],
                {"name": "ddak/flaskr/SECRET_KEY"},
            ),
            resource(
                "aws_iam_role_policy",
                "read",
                ["update"],
                {"role": role["name"], "policy": json.dumps(policy)},
            ),
        ],
    )


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.raw = plan_json()
        self.apply_code = 0
        self.outputs = {
            "secret_arn": {"value": SECRET.replace("??????", "ABC123"), "sensitive": False}
        }
        self.check_result = {
            "results": {"failed_checks": [], "skipped_checks": [], "passed_checks": []}
        }
        self.apply_timeout = False

    def run(self, argv, *, cwd, deadline, session=None):
        self.calls.append((argv, session))
        verb = argv[1]
        if Path(argv[0]).name == "checkov":
            return CommandResult(0, json.dumps(self.check_result))
        if verb == "plan":
            (cwd / "approved.tfplan").write_bytes(b"fake-saved-plan")
            return CommandResult(2, "")
        if verb == "show":
            return CommandResult(0, json.dumps(self.raw))
        if verb == "validate":
            return CommandResult(0, '{"valid": true}')
        if verb == "apply":
            if self.apply_timeout:
                raise DdakToolError(
                    ErrorCode.ADAPTER_TIMEOUT,
                    "fixture",
                )
            return CommandResult(self.apply_code, "not exposed")
        if verb == "output":
            return CommandResult(0, json.dumps(self.outputs))
        return CommandResult(0, "")


@pytest.fixture
def runtime(tmp_path):
    fake, approvals, guard = FakeRunner(), [], Mock()
    instance = InfraRuntime(
        root=tmp_path,
        run_id="run-1",
        settings=SETTINGS,
        lock_file=b"fixture-lock",
        runner=fake,
        approvals=lambda: approvals,
        guard=guard,
    )
    return instance, fake, approvals, guard


def planned(runtime):
    instance, _fake, approvals, _guard = runtime
    assert instance.validate({"main.tf": HCL}).passed
    summary = instance.plan(
        session=SESSION, analyzer=Mock(validate_policy=Mock(return_value={"findings": []}))
    )
    approvals.append(approval(summary["plan_sha256"]))
    return summary


def test_apply_error_detail_is_redacted_and_partial_evidence_preserved(runtime):
    planned(runtime)
    instance, fake, _approvals, _guard = runtime
    original = fake.run
    private_value = "fixture-private-" + "value"

    def fail_apply(argv, **kwargs):
        if argv[1] == "apply":
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "password=" + private_value)
        return original(argv, **kwargs)

    fake.run = fail_apply
    with pytest.raises(DdakToolError) as failure:
        instance.apply(session=SESSION)
    assert private_value not in str(failure.value)
    assert (instance.work / "apply-started").exists()
    assert (instance.work / "approved.tfplan").exists()
    with pytest.raises(DdakToolError, match="PRECONDITION_FAILED"):
        instance.apply(session=SESSION)


def test_approved_v2_summary_and_apply(runtime):
    summary = planned(runtime)
    instance, fake, _approvals, guard = runtime
    assert summary["counts"] == {"create": 1, "update": 1, "delete": 0, "replace": 0}
    assert ACCOUNT not in json.dumps(summary)
    assert encode_meta(summary, infra=True)
    result = instance.apply(session=SESSION)
    assert result["outputs"] == {"secret_arn": SECRET.replace("??????", "ABC123")}
    assert not (instance.work / "approved.tfplan").exists()
    assert not (instance.work / "checkov-plan.json").exists()
    with pytest.raises(DdakToolError):
        instance.apply(session=SESSION)
    assert sum(cmd[1] == "apply" for cmd, _ in fake.calls) == 1
    assert guard.call_count == 3
    # init(validate)/validate/Checkov는 자격증명 없이 실행한다.
    for _command, session in fake.calls[:3]:
        assert session is None


def test_plan_resolves_new_policy_role_from_configuration_reference():
    raw = plan_json()
    policy_change = raw["resource_changes"][2]["change"]
    policy_change["after"]["role"] = None
    policy_change["after_unknown"]["role"] = True
    raw["configuration"] = {
        "root_module": {
            "resources": [
                {
                    "address": "aws_iam_role_policy.read",
                    "mode": "managed",
                    "type": "aws_iam_role_policy",
                    "name": "read",
                    "expressions": {
                        "role": {"references": ["aws_iam_role.exec.id"]},
                    },
                }
            ]
        }
    }

    summary = summarize_plan(
        raw,
        layer="app",
        project="flaskr",
        plan_sha256=digest(b"unknown-role-id"),
        exit_code=2,
        account_id=ACCOUNT,
        boundary_arn=SETTINGS.boundary_arn,
        update=True,
        analyzer=Mock(validate_policy=Mock(return_value={"findings": []})),
        checkov={"passed": True, "failed": []},
    )

    assert summary["counts"]["update"] == 1
    assert summary["iam_diff"][0]["address"] == "aws_iam_role_policy.read"


def test_plan_rejects_unknown_policy_role_without_direct_reference():
    raw = plan_json()
    policy_change = raw["resource_changes"][2]["change"]
    policy_change["after"]["role"] = None
    policy_change["after_unknown"]["role"] = True

    with pytest.raises(PolicyViolation, match="IAM_ROLE_UNKNOWN"):
        summarize_plan(
            raw,
            layer="app",
            project="flaskr",
            plan_sha256=digest(b"unknown-role-id"),
            exit_code=2,
            account_id=ACCOUNT,
            boundary_arn=SETTINGS.boundary_arn,
            update=True,
            analyzer=Mock(validate_policy=Mock(return_value={"findings": []})),
            checkov={"passed": True, "failed": []},
        )


@pytest.mark.parametrize(
    "source",
    [
        'data "external" "x" { program = ["ignored"] }',
        'provider "aws" { region = "us-east-1" }',
        'module "x" { source = "./x" }',
        'resource "aws_secretsmanager_secret_version" "x" { secret_string = "value" }',
        'resource "aws_secretsmanager_secret" "x" { name = file("ignored") }',
        'resource "aws_secretsmanager_secret" "x" { provisioner "local-exec" {command="ignored"} }',
        "# checkov:skip=CKV_AWS_1:ignored\n" + HCL,
    ],
)
def test_static_failure_has_no_runner_calls(runtime, source):
    instance, fake, *_ = runtime
    assert not instance.validate({"main.tf": source}).passed
    assert not fake.calls


@pytest.mark.parametrize(
    "change", ["file", "extra", "symlink", "plan", "approval", "denied", "project", "run"]
)
def test_changed_or_unapproved_is_not_applied(runtime, change):
    planned(runtime)
    instance, fake, approvals, _ = runtime
    if change == "file":
        (instance.work / "main.tf").write_text(HCL + "# changed")
    elif change == "extra":
        (instance.work / "extra.auto.tfvars").write_text('project="other"')
    elif change == "symlink":
        (instance.work / "main.tf").unlink()
        (instance.work / "main.tf").symlink_to(instance.work / "ddak.tf.json")
    elif change == "plan":
        (instance.work / "approved.tfplan").write_bytes(b"changed")
    elif change == "approval":
        approvals.clear()
    else:
        edits = {
            "denied": {"decision": "denied"},
            "project": {"project": "other"},
            "run": {"run_id": "other"},
        }
        approvals[:] = [approval(approvals[0].bound_to, **edits[change])]
    with pytest.raises(DdakToolError):
        instance.apply(session=SESSION)
    assert not any(cmd[1] == "apply" for cmd, _ in fake.calls)


@pytest.mark.parametrize("failure", ["apply", "timeout", "output"])
def test_partial_or_unknown_apply_never_retried(runtime, failure):
    planned(runtime)
    instance, fake, *_ = runtime
    if failure == "apply":
        fake.apply_code = 1
    elif failure == "timeout":
        fake.apply_timeout = True
    else:
        fake.outputs["secret_arn"]["sensitive"] = True
    with pytest.raises(DdakToolError):
        instance.apply(session=SESSION)
    with pytest.raises(DdakToolError):
        instance.apply(session=SESSION)
    assert sum(cmd[1] == "apply" for cmd, _ in fake.calls) == 1
    assert (instance.work / "apply-started").exists()


@pytest.mark.parametrize(
    "failure", ["unknown", "delete", "replace", "analyzer", "sensitive", "outside", "checkov"]
)
def test_plan_blocks_unsafe_or_unverified(runtime, failure):
    instance, fake, *_ = runtime
    assert instance.validate({"main.tf": HCL}).passed
    analyzer = Mock(validate_policy=Mock(return_value={"findings": []}))
    policy = fake.raw["resource_changes"][-1]["change"]
    if failure == "unknown":
        policy["after_unknown"]["policy"] = True
    elif failure in ("delete", "replace"):
        fake.raw["resource_changes"][1]["change"]["actions"] = (
            ["delete"] if failure == "delete" else ["create", "delete"]
        )
    elif failure == "sensitive":
        policy["after_sensitive"]["policy"] = True
    elif failure == "outside":
        fake.raw["resource_changes"][0]["change"]["after"]["path"] = "/ddak/pipeline/"
    elif failure == "analyzer":
        analyzer.validate_policy.return_value = {"findings": [{"findingType": "ERROR"}]}
    else:
        fake.check_result["results"]["failed_checks"] = [{"check_id": "CKV_AWS_1"}]
    with pytest.raises(DdakToolError):
        instance.plan(session=SESSION, analyzer=analyzer)
    with pytest.raises(DdakToolError):
        instance.apply(session=SESSION)
    assert not (instance.work / "checkov-plan.json").exists()


def test_analyzer_pagination_not_ignored():
    client = Mock(
        validate_policy=Mock(
            side_effect=[
                {"findings": [], "nextToken": "next"},
                {"findings": [{"findingType": "ERROR"}]},
            ]
        )
    )
    with pytest.raises(PolicyViolation, match="ACCESS_ANALYZER_ERROR"):
        summarize_plan(
            plan_json(),
            layer="app",
            project="flaskr",
            plan_sha256=digest(b"x"),
            exit_code=2,
            account_id=ACCOUNT,
            boundary_arn=SETTINGS.boundary_arn,
            update=True,
            analyzer=client,
            checkov={"passed": True, "failed": []},
        )
    assert client.validate_policy.call_count == 2


def test_outputs_reject_sensitive_and_ignore_unlisted():
    data = {
        "safe": {"sensitive": False, "value": "endpoint"},
        "private": {"sensitive": True, "value": "redacted-fixture"},
    }
    assert filter_outputs(data, {"safe": "string"}) == {"safe": "endpoint"}
    with pytest.raises(PolicyViolation):
        filter_outputs(data, {"private": "string"})


def test_runner_does_not_inherit_ambient_credentials(tmp_path, monkeypatch):
    for name in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "TF_LOG", "TF_CLI_ARGS", "BASH_ENV"):
        monkeypatch.setenv(name, "do-not-forward")
    script = "import os,json; print(json.dumps(dict(os.environ)))"
    out = CommandRunner().run(
        [sys.executable, "-c", script], cwd=tmp_path, deadline=time.monotonic() + 3
    )
    env = json.loads(out.stdout)
    assert not any(
        name in env
        for name in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "TF_LOG", "TF_CLI_ARGS", "BASH_ENV")
    )
    assert env["HOME"] != os.environ["HOME"]
    assert not Path(env["HOME"]).exists()


def test_runner_timeout(tmp_path):
    with pytest.raises(DdakToolError, match="ADAPTER_TIMEOUT"):
        CommandRunner().run(
            [sys.executable, "-c", "import time; time.sleep(3)"],
            cwd=tmp_path,
            deadline=time.monotonic() + 0.1,
        )


def test_foundation_without_approval_has_no_aws_calls(tmp_path):
    s3, iam = Mock(), Mock()
    with pytest.raises(DdakToolError, match="APPROVAL_REQUIRED"):
        apply_foundation(
            settings=SETTINGS,
            run_id="run-1",
            s3=s3,
            iam=iam,
            sts=Mock(get_caller_identity=Mock(return_value={"Account": ACCOUNT})),
            approvals=lambda: [],
            guard=Mock(),
            marker=tmp_path / "started",
        )
    assert not s3.mock_calls and not iam.mock_calls


def test_foundation_reuses_only_matching_owned_resources(tmp_path):
    s3, iam = Mock(), Mock()
    template = foundation_template(SETTINGS)
    role = template["ecs_infrastructure_role"]
    iam.get_role.return_value = {
        "Role": {
            "RoleName": role["name"],
            "Path": role["path"],
            "Arn": role["arn"],
            "AssumeRolePolicyDocument": role["trust_policy"],
        }
    }
    iam.list_attached_role_policies.return_value = {"AttachedPolicies": [], "IsTruncated": False}
    iam.list_role_policies.return_value = {"PolicyNames": [], "IsTruncated": False}
    s3.get_bucket_tagging.return_value = {
        "TagSet": [{"Key": k, "Value": v} for k, v in template["tags"].items()]
    }
    s3.get_bucket_policy.return_value = {"Policy": json.dumps(template["bucket_policy"])}
    documents = {
        SETTINGS.boundary_arn: template["boundary"],
        SETTINGS.build_boundary_arn: template["build_boundary"],
    }
    iam.get_policy.side_effect = lambda **kw: {
        "Policy": {"Arn": kw["PolicyArn"], "DefaultVersionId": "v1"}
    }
    iam.get_policy_version.side_effect = lambda **kw: {
        "PolicyVersion": {
            "Document": documents[kw["PolicyArn"]],
            "VersionId": "v1",
            "IsDefaultVersion": True,
        }
    }
    snapshots = [
        {
            "policy_arn": arn,
            "default_version_id": "v1",
            "document_sha256": digest(canonical(document)),
            "document": document,
        }
        for arn, document in documents.items()
    ]
    records = [approval(foundation_approval_hash(SETTINGS, snapshots), "foundation")]
    result = apply_foundation(
        settings=SETTINGS,
        run_id="run-1",
        s3=s3,
        iam=iam,
        sts=Mock(get_caller_identity=Mock(return_value={"Account": ACCOUNT})),
        approvals=lambda: records,
        guard=Mock(),
        marker=tmp_path / "started",
        expected_boundaries=snapshots,
    )
    assert result["app_boundary_arn"] == SETTINGS.boundary_arn
    s3.create_bucket.assert_not_called()
    iam.create_policy.assert_not_called()
    s3.put_public_access_block.assert_called_once()


def test_review_fixes_static_codebuild_and_cidr():
    codebuild = """resource "aws_codebuild_project" "build" {
  name = "ddak-demo-build"
  environment { compute_type="BUILD_GENERAL1_SMALL"
    image="aws/codebuild/standard:7.0"
    type="LINUX_CONTAINER"
  }
}"""
    codebuild = codebuild.replace(
        '  name = "ddak-demo-build"',
        '  name = "ddak-demo-build"\n  source { type="GITHUB"\n buildspec='
        + json.dumps(OVERRIDE_REQUIRED_BUILDSPEC)
        + "\n }",
    )
    assert static_gate({"main.tf": codebuild}, layer="platform").passed
    assert not static_gate(
        {
            "main.tf": codebuild.replace(
                'type="LINUX_CONTAINER"',
                'type="LINUX_CONTAINER"\nenvironment_variable {name="token"\nvalue="fixture"}',
            )
        },
        layer="platform",
    ).passed
    sg = """resource "aws_vpc_security_group_ingress_rule" "db" {
    cidr_ipv4 = "not-a-cidr"
    from_port = 3306
    to_port = 3306
    ip_protocol = "tcp"
    }"""
    assert static_gate({"main.tf": sg}, layer="platform").detail == "CIDR_MUST_BE_LITERAL"


def test_certificate_validation_is_the_only_allowed_for_each():
    certificate_record = """resource "aws_route53_record" "certificate_validation" {
  for_each = {
    for d in aws_acm_certificate.main.domain_validation_options : d.domain_name => {
      name = d.resource_record_name
      type = d.resource_record_type
      record = d.resource_record_value
    }
  }
  zone_id = "Z123456"
  name = each.value.name
  type = each.value.type
  records = [each.value.record]
  ttl = 60
}"""
    assert static_gate({"main.tf": certificate_record}, layer="platform").passed
    assert not static_gate(
        {
            "main.tf": certificate_record.replace(
                'aws_route53_record" "certificate_validation', 'aws_route53_record" "app'
            )
        },
        layer="platform",
    ).passed
    assert not static_gate(
        {
            "main.tf": certificate_record.replace(
                "aws_acm_certificate.main.domain_validation_options", "var.records"
            )
        },
        layer="platform",
    ).passed


def test_generated_bundle_rejects_variables_not_supplied_by_code():
    allowed = 'resource "aws_s3_bucket" "source" { bucket = "${var.project}-source" }'
    unsupported = allowed.replace("var.project", "var.region")

    assert static_gate({"main.tf": allowed}, layer="platform").passed
    result = static_gate({"main.tf": unsupported}, layer="platform")
    assert not result.passed
    assert result.detail == "VARIABLE_NOT_ALLOWED"


def test_build_and_app_boundaries_are_distinct():
    from ddak.cloud.infra.providers.aws import boundary_document, build_boundary_document

    source = (Path(__file__).parents[3] / "fixtures" / "infra" / "app_v2.tf").read_text()
    role_only = source[
        source.index('resource "aws_iam_role"') : source.index('resource "aws_iam_role_policy"')
    ]
    build = (
        role_only.replace('"ddak-${var.project}-exec"', '"ddak-codebuild"')
        .replace("/ddak/app/", "/ddak/pipeline/")
        .replace("ecs-tasks.amazonaws.com", "codebuild.amazonaws.com")
    )
    assert not static_gate({"main.tf": build}, layer="platform").passed
    assert static_gate(
        {"main.tf": build.replace("var.app_boundary_arn", "var.build_boundary_arn")},
        layer="platform",
    ).passed
    assert "dockerhub-push" not in json.dumps(boundary_document(ACCOUNT, SETTINGS.project))
    assert "dockerhub-push" in json.dumps(build_boundary_document(ACCOUNT))
    assert SETTINGS.boundary_arn != SETTINGS.build_boundary_arn


def test_approval_wait_is_not_execution_timeout(runtime):
    planned(runtime)
    instance, _, _, _ = runtime
    instance.deadline = time.monotonic() - 1000
    assert instance.apply(session=SESSION)["outputs"]


def test_expired_guard_does_not_mark_apply_started(runtime):
    planned(runtime)
    instance, fake, *_ = runtime

    def expired_guard():
        instance.deadline = time.monotonic() - 1

    instance._guard = expired_guard
    with pytest.raises(DdakToolError, match="ADAPTER_TIMEOUT"):
        instance.apply(session=SESSION)
    assert not (instance.work / "apply-started").exists()
    assert not instance._consumed
    assert not any(command[1] == "apply" for command, _ in fake.calls)


@pytest.mark.parametrize(
    "resource_address,passed",
    [
        ("aws_security_group.alb", True),
        ("aws_vpc_security_group_ingress_rule.alb_http", True),
        ("aws_security_group.db", False),
        ("aws_vpc_security_group_ingress_rule.db_http", False),
    ],
)
def test_checkov_exception_is_resource_and_check_specific(tmp_path, resource_address, passed):
    from dataclasses import replace

    fake = FakeRunner()
    fake.check_result["results"]["failed_checks"] = [
        {"check_id": "CKV_AWS_260", "resource": resource_address}
    ]
    settings = replace(
        SETTINGS,
        alb_security_group_addresses=(
            "aws_security_group.alb",
            "aws_vpc_security_group_ingress_rule.alb_http",
        ),
    )
    runtime = InfraRuntime(
        root=tmp_path,
        run_id="run-1",
        settings=settings,
        lock_file=b"fixture",
        runner=fake,
        approvals=lambda: [],
        guard=Mock(),
    )
    assert runtime.validate({"main.tf": HCL}).passed is passed
    assert all("--quiet" not in argv for argv, _ in fake.calls)


@pytest.mark.parametrize(
    "role,passed", [("ddak-flaskr-exec", False), ("ddak-flaskr-dbinit-exec", True)]
)
def test_rds_master_secret_only_dbinit(role, passed):
    raw = plan_json()
    raw["resource_changes"][0]["change"]["after"]["name"] = role
    change = raw["resource_changes"][2]["change"]["after"]
    change["role"] = role
    policy = json.loads(change["policy"])
    policy["Statement"][0]["Resource"] = [
        f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:rds!fixture"
    ]
    change["policy"] = json.dumps(policy)
    kwargs = dict(
        rds_master_secret_arn=f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:rds!fixture",
        layer="app",
        project="flaskr",
        plan_sha256=digest(b"x"),
        exit_code=2,
        account_id=ACCOUNT,
        boundary_arn=SETTINGS.boundary_arn,
        update=True,
        analyzer=Mock(validate_policy=Mock(return_value={"findings": []})),
        checkov={"passed": True, "failed": []},
    )
    if passed:
        assert summarize_plan(raw, **kwargs)
    else:
        with pytest.raises(PolicyViolation, match="ROLE_SECRET_SCOPE"):
            summarize_plan(raw, **kwargs)


def test_iam_policy_diff_contains_removed_denies():
    raw = plan_json()
    change = raw["resource_changes"][2]["change"]
    before = json.loads(change["after"]["policy"])
    before["Statement"].append(
        {"Effect": "Deny", "Action": "secretsmanager:GetSecretValue", "Resource": SECRET}
    )
    change["before"] = {"policy": json.dumps(before)}
    summary = summarize_plan(
        raw,
        layer="app",
        project="flaskr",
        plan_sha256=digest(b"x"),
        exit_code=2,
        account_id=ACCOUNT,
        boundary_arn=SETTINGS.boundary_arn,
        update=True,
        analyzer=Mock(validate_policy=Mock(return_value={"findings": []})),
        checkov={"passed": True, "failed": []},
    )
    row = summary["iam_diff"][0]
    assert row["before"][-1]["Effect"] == "Deny"
    assert all(s["Effect"] != "Deny" for s in row["after"])
    assert ACCOUNT not in json.dumps(summary)
    assert encode_meta(summary, infra=True)


def test_foundation_wrong_account_stops_before_resource_access(tmp_path):
    s3, iam = Mock(), Mock()
    records = [approval(foundation_approval_hash(SETTINGS, []), "foundation")]
    with pytest.raises(DdakToolError, match="PRECONDITION_FAILED"):
        apply_foundation(
            settings=SETTINGS,
            run_id="run-1",
            s3=s3,
            iam=iam,
            sts=Mock(get_caller_identity=Mock(return_value={"Account": "999999999999"})),
            approvals=lambda: records,
            guard=Mock(),
            marker=tmp_path / "started",
            expected_boundaries=[],
        )
    assert not s3.mock_calls and not iam.mock_calls
    assert not (tmp_path / "started").exists()


def test_malformed_output_is_controlled_error(runtime):
    instance, fake, *_ = runtime
    fake.outputs = []
    with pytest.raises(DdakToolError, match="ADAPTER_FAILED"):
        instance.refresh(session=SESSION)


@pytest.mark.parametrize(
    "action",
    [
        "secretsmanager:getsecretvalue",
        "SecretsManager:GetSecretValue",
        "SECRETSMANAGER:GETSECRETVALUE",
    ],
)
def test_secret_scope_uses_iam_case_insensitive_action_semantics(action):
    raw = plan_json()
    change = raw["resource_changes"][2]["change"]["after"]
    policy = json.loads(change["policy"])
    policy["Statement"][0]["Action"] = [action]
    policy["Statement"][0]["Resource"] = [
        f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:rds!fixture"
    ]
    change["policy"] = json.dumps(policy)
    with pytest.raises(PolicyViolation, match="ROLE_SECRET_SCOPE"):
        summarize_plan(
            raw,
            layer="app",
            project="flaskr",
            plan_sha256=digest(b"x"),
            exit_code=2,
            account_id=ACCOUNT,
            boundary_arn=SETTINGS.boundary_arn,
            update=True,
            analyzer=Mock(validate_policy=Mock(return_value={"findings": []})),
            checkov={"passed": True, "failed": []},
        )


def test_codebuild_boundary_shown_as_attached():
    raw = plan_json()
    role = raw["resource_changes"][0]["change"]["after"]
    role.update(
        name="ddak-codebuild",
        path="/ddak/pipeline/",
        permissions_boundary=SETTINGS.build_boundary_arn,
    )
    change = raw["resource_changes"][2]["change"]["after"]
    change["role"] = "ddak-codebuild"
    policy = json.loads(change["policy"])
    policy["Statement"][0]["Resource"] = [
        f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:ddak-platform/dockerhub-push-??????"
    ]
    change["policy"] = json.dumps(policy)
    summary = summarize_plan(
        raw,
        layer="platform",
        project="flaskr",
        plan_sha256=digest(b"x"),
        exit_code=2,
        account_id=ACCOUNT,
        boundary_arn=SETTINGS.boundary_arn,
        build_boundary_arn=SETTINGS.build_boundary_arn,
        update=False,
        analyzer=Mock(validate_policy=Mock(return_value={"findings": []})),
        checkov={"passed": True, "failed": []},
    )
    assert summary["iam_diff"][0]["boundary_attached"] is True
    assert encode_meta(summary, infra=True)


@pytest.mark.parametrize(
    "attribute", ['region = "us-east-1"', 'replica { region = "us-east-1" }', 'policy = "ignored"']
)
def test_secret_rejects_region_replica_and_policy(attribute):
    source = HCL.replace("  name =", attribute + "\n  name =")
    assert not static_gate({"main.tf": source}, layer="app").passed


@pytest.mark.parametrize("cidr", ["10.0.0.0/0", "0::/0", "0.0.0.0/1", "128.0.0.0/1", "8.8.8.0/24"])
def test_mysql_allows_only_private_cidrs(cidr):
    attr = "cidr_ipv6" if ":" in cidr else "cidr_ipv4"
    source = f'''resource "aws_vpc_security_group_ingress_rule" "db" {{
    {attr} = "{cidr}"
    from_port = 3306
    to_port = 3306
    ip_protocol = "tcp"
    }}'''
    assert static_gate({"main.tf": source}, layer="platform").detail == "MYSQL_PUBLIC"


@pytest.mark.parametrize(
    "key",
    [
        "environment",
        "Environment",
        "ENVIRONMENT",
        "environmentFiles",
        "ENVIRONMENTFILES",
        "secrets",
        "Secrets",
    ],
)
def test_plaintext_container_environment_rejected(key):
    source = """resource "aws_ecs_task_definition" "app" {
    container_definitions = jsonencode([{name="web",
    environment=[{name="PASSWORD",value="fixture"}]}])
    }"""
    source = source.replace("environment=", key + "=")
    assert (
        static_gate({"main.tf": source}, layer="platform").detail == "CONTAINER_ENV_MANAGED_BY_C2"
    )


@pytest.mark.parametrize("change", ["kind", "hash", "later_denial"])
def test_approval_identity_and_time_order(runtime, change):
    from datetime import timedelta

    planned(runtime)
    instance, fake, records, _ = runtime
    original = records[0]
    if change == "kind":
        records[0] = original.model_copy(update={"kind": "deploy"})
    elif change == "hash":
        records[0] = original.model_copy(update={"bound_to": digest(b"other")})
    else:
        records.insert(
            0,
            original.model_copy(
                update={
                    "decision": "denied",
                    "approved_at": original.approved_at + timedelta(seconds=1),
                }
            ),
        )
    with pytest.raises(DdakToolError, match="APPROVAL_REQUIRED"):
        instance.apply(session=SESSION)
    assert not any(cmd[1] == "apply" for cmd, _ in fake.calls)


def test_same_run_second_instance_cannot_apply(tmp_path):
    records = []
    fake = FakeRunner()

    def new():
        return InfraRuntime(
            root=tmp_path,
            run_id="run-1",
            settings=SETTINGS,
            lock_file=b"fixture",
            runner=fake,
            approvals=lambda: records,
            guard=Mock(),
        )

    one = new()
    summary = planned((one, fake, records, Mock()))
    one.apply(session=SESSION)
    one.close()
    two = new()
    planned((two, fake, records, Mock()))
    with pytest.raises(DdakToolError, match="PRECONDITION_FAILED"):
        two.apply(session=SESSION)
    assert sum(cmd[1] == "apply" for cmd, _ in fake.calls) == 1
    assert records[-1].bound_to == summary["plan_sha256"]


def test_cleanup_keeps_uncertain_apply(runtime):
    planned(runtime)
    instance, fake, *_ = runtime
    fake.apply_code = 1
    with pytest.raises(DdakToolError):
        instance.apply(session=SESSION)
    with pytest.raises(DdakToolError, match="보존"):
        instance.close()
    assert (instance.work / "apply-started").exists()


def test_refresh_has_fresh_read_budget(runtime):
    instance, *_ = runtime
    instance.deadline = time.monotonic() - 1000
    assert instance.refresh(session=SESSION) == {"secret_arn": SECRET.replace("??????", "ABC123")}
    assert instance.deadline > time.monotonic()


def test_timeout_sends_interrupt_before_kill(tmp_path, monkeypatch):
    import signal
    import subprocess

    process = Mock(pid=123456)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 1), 0, 0]
    interrupt = Mock(side_effect=[None, ProcessLookupError()])
    monkeypatch.setattr("ddak.cloud.infra.runtime.subprocess.Popen", Mock(return_value=process))
    monkeypatch.setattr("ddak.cloud.infra.runtime.os.killpg", interrupt)
    with pytest.raises(DdakToolError, match="ADAPTER_TIMEOUT"):
        CommandRunner().run(["fixture-cli"], cwd=tmp_path, deadline=time.monotonic() + 1)
    assert [call.args for call in interrupt.call_args_list] == [
        (process.pid, signal.SIGINT),
        (process.pid, 0),
    ]


@pytest.mark.parametrize("delimiter", ["<<EOT", "<<-EOT"])
def test_opaque_multiline_templates_rejected_before_cli(runtime, delimiter):
    instance, fake, *_ = runtime
    source = 'resource "aws_cloudwatch_log_group" "app" { name = ' + delimiter + "\nplain\nEOT\n}"
    assert instance.validate({"main.tf": source}).detail == "HEREDOC_NOT_ALLOWED"
    assert not fake.calls


def test_apply_timeout_waits_for_configured_grace_without_second_interrupt(tmp_path, monkeypatch):
    import signal
    import subprocess

    process = Mock(pid=123456)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("terraform", 1), 0, 0]
    popen = Mock(return_value=process)
    kill = Mock(side_effect=[None, ProcessLookupError()])
    monkeypatch.setattr("ddak.cloud.infra.runtime.subprocess.Popen", popen)
    monkeypatch.setattr("ddak.cloud.infra.runtime.os.killpg", kill)
    with pytest.raises(DdakToolError, match="ADAPTER_TIMEOUT"):
        CommandRunner(apply_stop_grace=180).run(
            ["terraform", "apply"], cwd=tmp_path, deadline=time.monotonic() + 1
        )
    assert process.wait.call_args_list[1].kwargs == {"timeout": 180}
    assert [c.args for c in kill.call_args_list] == [
        (process.pid, signal.SIGINT),
        (process.pid, 0),
    ]


def test_timeout_terminates_group_when_parent_exits_first(tmp_path, monkeypatch):
    import signal
    import subprocess

    process = Mock(pid=123456)
    process.poll.return_value = 0
    process.wait.side_effect = [subprocess.TimeoutExpired("terraform", 1), 0, 0]
    monkeypatch.setattr("ddak.cloud.infra.runtime.subprocess.Popen", Mock(return_value=process))
    kill = Mock()
    monkeypatch.setattr("ddak.cloud.infra.runtime.os.killpg", kill)
    monkeypatch.setattr("ddak.cloud.infra.runtime.time.monotonic", Mock(side_effect=[0, 0, 6]))
    with pytest.raises(DdakToolError, match="ADAPTER_TIMEOUT"):
        CommandRunner(stop_grace=5).run(["terraform", "plan"], cwd=tmp_path, deadline=1)
    assert [c.args for c in kill.call_args_list] == [
        (process.pid, signal.SIGINT),
        (process.pid, 0),
        (process.pid, signal.SIGKILL),
    ]


def test_rds_scope_requires_exact_configured_secret():
    raw = plan_json()
    role = raw["resource_changes"][0]["change"]["after"]
    role["name"] = "ddak-flaskr-dbinit-exec"
    after = raw["resource_changes"][2]["change"]["after"]
    after["role"] = role["name"]
    policy = json.loads(after["policy"])
    policy["Statement"][0]["Resource"] = [
        f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:rds!*"
    ]
    after["policy"] = json.dumps(policy)
    with pytest.raises(PolicyViolation, match="ROLE_SECRET_SCOPE"):
        summarize_plan(
            raw,
            layer="app",
            project="flaskr",
            plan_sha256=digest(b"x"),
            exit_code=2,
            account_id=ACCOUNT,
            boundary_arn=SETTINGS.boundary_arn,
            update=True,
            rds_master_secret_arn=f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:rds!one",
            analyzer=Mock(validate_policy=Mock(return_value={"findings": []})),
            checkov={"passed": True, "failed": []},
        )
