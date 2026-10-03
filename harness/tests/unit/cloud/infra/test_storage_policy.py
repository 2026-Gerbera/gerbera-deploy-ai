"""업로드 저장소 관문. HCL·plan fixture와 가짜 정책 분석기만 사용한다."""

import json
import socket
import subprocess
from copy import deepcopy
from unittest.mock import Mock

import pytest

from ddak.cloud.infra.plan import summarize_plan
from ddak.cloud.infra.policy import PolicyViolation, static_gate
from ddak.cloud.infra.providers.aws import boundary_document
from ddak.cloud.infra.storage_policy import STORAGE_ADDRESSES
from ddak.core.storage import bucket_name, bucket_prefix

ACCOUNT = "123456789012"
PROJECT = "flaskr"
BUCKET = bucket_name(PROJECT, 1)
VARIABLE = "${var.upload_bucket}"
BOUNDARY = f"arn:aws:iam::{ACCOUNT}:policy/ddak/boundary/ddak-app-boundary"
ROLES = {
    "flaskr-task": {
        "RoleName": "flaskr-task",
        "Path": "/ddak/app/",
        "Arn": f"arn:aws:iam::{ACCOUNT}:role/ddak/app/flaskr-task",
        "PermissionsBoundary": {"PermissionsBoundaryArn": BOUNDARY},
    }
}


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("실제 외부 호출 금지")

    monkeypatch.setattr("boto3.Session", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def document(bucket=BUCKET):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": f"arn:aws:s3:::{bucket}/*",
            },
            {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}"},
        ],
    }


def hcl(*, bucket=VARIABLE, role="flaskr-task", policy=None):
    policy = document(bucket) if policy is None else policy
    return {
        "storage.tf": f'''
resource "aws_s3_bucket" "uploads" {{
  bucket = "{bucket}"
  force_destroy = true
}}
resource "aws_s3_bucket_public_access_block" "uploads" {{
  bucket = aws_s3_bucket.uploads.id
  block_public_acls = true
  block_public_policy = true
  ignore_public_acls = true
  restrict_public_buckets = true
}}
resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {{
  bucket = aws_s3_bucket.uploads.id
  rule {{
    apply_server_side_encryption_by_default {{
      sse_algorithm = "AES256"
    }}
  }}
}}
resource "aws_iam_role_policy" "uploads" {{
  name = "uploads"
  role = "{role}"
  policy = jsonencode({json.dumps(policy)})
}}
'''
    }


def gate(files=None, **overrides):
    kwargs = dict(
        layer="app",
        storage_intent="create",
        storage_bucket=BUCKET,
        project=PROJECT,
        account_id=ACCOUNT,
    )
    kwargs.update(overrides)
    return static_gate(hcl() if files is None else files, **kwargs)


def plan(actions=None, *, bucket=BUCKET):
    actions = ["create"] if actions is None else actions
    bodies = {
        "aws_s3_bucket.uploads": {"bucket": bucket, "force_destroy": True},
        "aws_s3_bucket_public_access_block.uploads": {
            "bucket": bucket,
            "block_public_acls": True,
            "block_public_policy": True,
            "ignore_public_acls": True,
            "restrict_public_buckets": True,
        },
        "aws_s3_bucket_server_side_encryption_configuration.uploads": {
            "bucket": bucket,
            "rule": [
                {
                    "apply_server_side_encryption_by_default": [{"sse_algorithm": "AES256"}],
                }
            ],
        },
        "aws_iam_role_policy.uploads": {
            "name": "uploads",
            "role": "flaskr-task",
            "policy": json.dumps(document(bucket)),
        },
    }
    return {
        "format_version": "1.2",
        "resource_changes": [
            {
                "type": address.split(".")[0],
                "name": "uploads",
                "address": address,
                "mode": "managed",
                "change": {
                    "actions": list(actions),
                    "after": None if actions == ["delete"] else deepcopy(body),
                    "before": deepcopy(body) if actions != ["create"] else None,
                    "after_unknown": {},
                    "after_sensitive": {},
                    "before_sensitive": {},
                },
            }
            for address, body in bodies.items()
        ],
    }


def summarize(raw=None, **overrides):
    kwargs = dict(
        layer="app",
        storage_intent="create",
        storage_bucket=BUCKET,
        update=True,
        external_roles=deepcopy(ROLES),
        account_id=ACCOUNT,
        boundary_arn=BOUNDARY,
        project=PROJECT,
        plan_sha256="a" * 64,
        exit_code=2,
        analyzer=Mock(validate_policy=Mock(return_value={"findings": []})),
        checkov={"passed": True, "failed": []},
    )
    kwargs.update(overrides)
    return summarize_plan(plan() if raw is None else raw, **kwargs)


def test_create_four_and_exact_boundary_scope():
    assert gate().passed
    summary = summarize()
    assert summary["counts"] == {"create": 4, "update": 0, "delete": 0, "replace": 0}
    assert not summary["destructive"]
    assert summary["iam_diff"][0]["boundary_attached"]
    expected = document(bucket_prefix(PROJECT) + "*")["Statement"]
    observed = [
        row
        for row in boundary_document(ACCOUNT, PROJECT)["Statement"]
        if row["Effect"] == "Allow" and str(row["Action"]).startswith(("s3:", "['s3:"))
    ]
    assert observed == expected


def test_hcl_template_names():
    assert gate(hcl(role="${var.project}-task")).passed


@pytest.mark.parametrize("project", ["flaskr", "inventory-api"])
def test_boundary_storage_scope_is_only_platform_prefix(project):
    expected = document(bucket_prefix(project) + "*")["Statement"]
    for account in (ACCOUNT, "999999999999"):
        observed = [
            row
            for row in boundary_document(account, project)["Statement"]
            if any(
                action.startswith("s3:")
                for action in (
                    row["Action"] if isinstance(row["Action"], list) else [row["Action"]]
                )
            )
        ]
        assert observed == expected


@pytest.mark.parametrize("n", [1, 2, 999999])
def test_numbered_bucket_uses_same_variable_hcl(n):
    bucket = bucket_name(PROJECT, n)
    assert gate(storage_bucket=bucket).passed
    assert summarize(plan(bucket=bucket), storage_bucket=bucket)["counts"]["create"] == 4


@pytest.mark.parametrize(
    "bucket",
    [
        None,
        "",
        "gerbera-other-images-1",
        "gerbera-flaskr-images-x-images-1",
        "gerbera-flaskr-images-0",
        "gerbera-flaskr-images-01",
        "gerbera-flaskr-images-1000000",
        "gerbera-flaskr-images-2-extra",
        "gerbera-flaskr-images-2\n",
        f"ddak-flaskr-uploads-{ACCOUNT}",
    ],
)
@pytest.mark.parametrize("intent", ["create", "remove"])
def test_reserved_bucket_must_match_project_and_pattern(bucket, intent):
    files = hcl() if intent == "create" else {}
    assert gate(files, storage_intent=intent, storage_bucket=bucket).detail == "STORAGE_NAME_SCOPE"
    with pytest.raises(PolicyViolation, match="STORAGE_NAME_SCOPE"):
        summarize(
            plan(["delete"] if intent == "remove" else ["create"]),
            storage_intent=intent,
            storage_bucket=bucket,
        )


@pytest.mark.parametrize("bucket", [BUCKET, "ddak-${var.project}-uploads-${var.account_id}"])
def test_hcl_bucket_must_read_owned_variable(bucket):
    assert gate(hcl(bucket=bucket)).detail == "STORAGE_NAME_SCOPE"


def test_hcl_dependent_buckets_can_read_owned_variable():
    files = hcl()
    files["storage.tf"] = files["storage.tf"].replace(
        "aws_s3_bucket.uploads.id", "var.upload_bucket"
    )
    assert gate(files).passed
    files["storage.tf"] += '\nvariable "upload_bucket" { type = string }\n'
    assert gate(files).detail == "RESOURCE_BLOCKS_ONLY"


@pytest.mark.parametrize("bucket", [BUCKET, "${var.upload_bucket}-other"])
def test_hcl_dependent_buckets_cannot_use_literal_or_modified_variable(bucket):
    files = hcl()
    files["storage.tf"] = files["storage.tf"].replace("aws_s3_bucket.uploads.id", f'"{bucket}"')
    assert gate(files).detail == "STORAGE_NAME_SCOPE"


@pytest.mark.parametrize("intent", ["create", "remove"])
def test_valid_second_bucket_cannot_replace_approved_bucket(intent):
    second = bucket_name(PROJECT, 2)
    raw = plan(["delete"] if intent == "remove" else ["create"], bucket=second)
    with pytest.raises(PolicyViolation, match="STORAGE_NAME_SCOPE"):
        summarize(raw, storage_intent=intent)
    assert summarize(raw, storage_intent=intent, storage_bucket=second)


def test_update_before_bucket_and_policy_must_match_reservation():
    second = bucket_name(PROJECT, 2)
    raw = plan(["update"], bucket=second)
    raw["resource_changes"][0]["change"]["before"]["bucket"] = BUCKET
    with pytest.raises(PolicyViolation, match="STORAGE_NAME_SCOPE"):
        summarize(raw, storage_bucket=second)
    raw = plan(bucket=second)
    raw["resource_changes"][-1]["change"]["after"]["policy"] = json.dumps(document())
    with pytest.raises(PolicyViolation, match="STORAGE_POLICY_SCOPE"):
        summarize(raw, storage_bucket=second)


@pytest.mark.parametrize("files", [{}, {"storage.tf": "# 저장소 제거\n"}])
def test_empty_storage_remove_hcl(files):
    assert gate(files, storage_intent="remove").passed
    assert not gate(files, storage_intent=None).passed


def test_remove_four_displays_deleted_policy_and_keeps_destructive_visible():
    summary = summarize(plan(["delete"]), storage_intent="remove")
    assert summary["counts"] == {"create": 0, "update": 0, "delete": 4, "replace": 0}
    assert set(summary["destructive"]) == STORAGE_ADDRESSES
    row = summary["iam_diff"][0]
    assert row["action"] == "delete" and row["before"]
    assert row["after"] == row["proposed_allow"] == []
    assert ACCOUNT not in json.dumps(summary)


@pytest.mark.parametrize(
    "overrides",
    [
        {"storage_intent": None},
        {"storage_intent": "create"},
        {"update": False},
        {"layer": "platform"},
        {"external_roles": None},
    ],
)
def test_delete_exception_requires_all_conditions(overrides):
    kwargs = dict(storage_intent="remove")
    kwargs.update(overrides)
    with pytest.raises(PolicyViolation):
        summarize(plan(["delete"]), **kwargs)


@pytest.mark.parametrize(
    "actions", [["delete", "create"], ["create", "delete"], ["update"], ["no-op"]]
)
def test_remove_rejects_nonpure_delete(actions):
    raw = plan(["delete"])
    raw["resource_changes"][0]["change"]["actions"] = actions
    with pytest.raises(PolicyViolation, match="UPDATE_DESTRUCTIVE"):
        summarize(raw, storage_intent="remove")


@pytest.mark.parametrize("actions", [["delete"], ["delete", "create"], ["create", "delete"]])
def test_create_rejects_destructive(actions):
    with pytest.raises(PolicyViolation, match="UPDATE_DESTRUCTIVE"):
        summarize(plan(actions))


@pytest.mark.parametrize("intent", ["create", "remove"])
def test_four_resources_required(intent):
    raw = plan(["delete"] if intent == "remove" else ["create"])
    raw["resource_changes"].pop()
    with pytest.raises(PolicyViolation, match="STORAGE_RESOURCE_SET"):
        summarize(raw, storage_intent=intent)


@pytest.mark.parametrize("kind", ["aws_secretsmanager_secret", "aws_iam_role_policy"])
@pytest.mark.parametrize("actions", [["delete"], ["delete", "create"], ["create", "delete"]])
def test_other_delete_and_replace_still_rejected(kind, actions):
    raw = plan(["delete"])
    raw["resource_changes"].append(
        {
            "type": kind,
            "address": f"{kind}.other",
            "mode": "managed",
            "change": {"actions": actions, "before": {}, "after": {}},
        }
    )
    with pytest.raises(PolicyViolation, match=r"IAM_DESTRUCTIVE|UPDATE_DESTRUCTIVE"):
        summarize(raw, storage_intent="remove")


@pytest.mark.parametrize(
    "replace_from,replace_to",
    [
        ('"uploads"', '"other"'),
        ("force_destroy = true", "force_destroy = false"),
        ("block_public_acls = true", "block_public_acls = false"),
        ("block_public_policy = true", "block_public_policy = false"),
        ("ignore_public_acls = true", "ignore_public_acls = false"),
        ("restrict_public_buckets = true", "restrict_public_buckets = false"),
        ('"AES256"', '"aws:kms"'),
        ("aws_s3_bucket.uploads.id", "aws_s3_bucket.other.id"),
    ],
)
def test_hcl_rejects_changed_addresses_and_settings(replace_from, replace_to):
    files = hcl()
    files["storage.tf"] = files["storage.tf"].replace(replace_from, replace_to)
    assert not gate(files).passed


@pytest.mark.parametrize("value", ["other-task", "ddak-codebuild", "flaskr-task-other"])
def test_hcl_role_scope(value):
    assert not gate(hcl(role=value)).passed


@pytest.mark.parametrize(
    "value", ["other", f"ddak-other-uploads-{ACCOUNT}", "ddak-flaskr-uploads-999999999999"]
)
def test_bucket_name_scope(value):
    assert not gate(hcl(bucket=value)).passed


@pytest.mark.parametrize(
    "mutation", ["action", "object_scope", "bucket_scope", "extra", "missing", "condition"]
)
@pytest.mark.parametrize("deleting", [False, True])
def test_exact_policy_scope_for_creation_and_removal(mutation, deleting):
    policy = document()
    if mutation == "action":
        policy["Statement"][0]["Action"].append("s3:DeleteObject")
    elif mutation == "object_scope":
        policy["Statement"][0]["Resource"] = f"arn:aws:s3:::{BUCKET}"
    elif mutation == "bucket_scope":
        policy["Statement"][1]["Resource"] += "/*"
    elif mutation == "extra":
        policy["Statement"].append(
            {"Effect": "Allow", "Action": "logs:PutLogEvents", "Resource": "*"}
        )
    elif mutation == "missing":
        policy["Statement"].pop()
    else:
        policy["Statement"][0]["Condition"] = {"StringEquals": {"fixture": "value"}}
    variable_policy = json.loads(json.dumps(policy).replace(BUCKET, VARIABLE))
    assert not gate(hcl(policy=variable_policy)).passed
    raw = plan(["delete"] if deleting else ["create"])
    raw["resource_changes"][-1]["change"]["before" if deleting else "after"]["policy"] = json.dumps(
        policy
    )
    with pytest.raises(PolicyViolation, match="STORAGE_POLICY_SCOPE"):
        summarize(raw, storage_intent="remove" if deleting else "create")


@pytest.mark.parametrize(
    "field,value",
    [
        ("RoleName", "other-task"),
        ("Path", "/ddak/pipeline/"),
        ("Arn", f"arn:aws:iam::{ACCOUNT}:role/ddak/app/other-task"),
        ("Arn", "arn:aws:iam::999999999999:role/ddak/app/flaskr-task"),
        ("PermissionsBoundary", {"PermissionsBoundaryArn": BOUNDARY + "-other"}),
        ("PermissionsBoundary", None),
    ],
)
def test_external_getrole_metadata_must_match(field, value):
    roles = deepcopy(ROLES)
    roles["flaskr-task"][field] = value
    with pytest.raises(PolicyViolation):
        summarize(external_roles=roles)


@pytest.mark.parametrize("deleting", [False, True])
@pytest.mark.parametrize(
    "index,key,value",
    [
        (0, "bucket", "other"),
        (0, "force_destroy", False),
        (1, "bucket", "other"),
        (1, "block_public_policy", False),
        (2, "bucket", "other"),
        (3, "name", "other"),
        (3, "role", "other-task"),
    ],
)
def test_plan_settings_and_before_scope(index, key, value, deleting):
    raw = plan(["delete"] if deleting else ["create"])
    raw["resource_changes"][index]["change"]["before" if deleting else "after"][key] = value
    with pytest.raises(PolicyViolation):
        summarize(raw, storage_intent="remove" if deleting else "create")


def test_unknown_bucket_requires_exact_configuration_reference():
    raw = plan()
    rows = raw["resource_changes"][1:3]
    for row in rows:
        row["change"]["after"]["bucket"] = None
        row["change"]["after_unknown"] = {"bucket": True}
    raw["configuration"] = {
        "root_module": {
            "resources": [
                {
                    "type": row["type"],
                    "address": row["address"],
                    "expressions": {
                        "bucket": {
                            "references": ["aws_s3_bucket.uploads.id", "aws_s3_bucket.uploads"]
                        },
                    },
                }
                for row in rows
            ]
        }
    }
    assert summarize(raw)["counts"]["create"] == 4
    raw["configuration"]["root_module"]["resources"][0]["expressions"]["bucket"] = {
        "references": ["aws_s3_bucket.other.id"],
    }
    with pytest.raises(PolicyViolation, match="STORAGE_NAME_SCOPE"):
        summarize(raw)


@pytest.mark.parametrize("field", ["role", "policy", "name"])
def test_unknown_policy_fields_rejected(field):
    raw = plan()
    raw["resource_changes"][-1]["change"]["after_unknown"][field] = True
    with pytest.raises(PolicyViolation, match="STORAGE_RESOURCE_UNKNOWN"):
        summarize(raw)


def test_external_role_permission_is_only_for_uploads_policy():
    raw = plan()
    other = deepcopy(raw["resource_changes"][-1])
    other.update(address="aws_iam_role_policy.other", name="other")
    other["change"]["after"]["policy"] = json.dumps(
        {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "secretsmanager:GetSecretValue",
                    "Resource": (
                        f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:"
                        "secret:ddak/flaskr/KEY-??????"
                    ),
                }
            ]
        }
    )
    raw["resource_changes"].append(other)
    with pytest.raises(PolicyViolation, match="IAM_ROLE_UNKNOWN"):
        summarize(raw)


def test_storage_cannot_own_foundation_bucket():
    assert gate(state_bucket=BUCKET).detail == "FOUNDATION_BUCKET_OWNED_BY_CODE"
    with pytest.raises(PolicyViolation, match="FOUNDATION_BUCKET_OWNED_BY_CODE"):
        summarize(state_bucket=BUCKET)


def test_ordinary_app_without_storage_is_unchanged():
    files = {
        "app.tf": """resource "aws_secretsmanager_secret" "session" {
      name = "ddak/${var.project}/SECRET_KEY"
    }"""
    }
    assert gate(files, storage_intent=None).passed
    assert (
        summarize(
            {"format_version": "1.2", "resource_changes": []},
            storage_intent=None,
            external_roles=None,
        )["counts"]["create"]
        == 0
    )
