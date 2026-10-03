"""ECS 기반 역할과 승인 경로만 검사한다. caller 정책 공급은 검증하지 않는다."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from botocore.exceptions import ClientError
from hcl2.api import loads

from ddak.cloud.infra import foundation
from ddak.cloud.infra.policy import policy_json
from ddak.cloud.infra.providers.aws import boundary_document
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.executor.approval_meta import encode_meta
from tests.unit.cloud.infra import test_runtime as f
from tests.unit.cloud.test_aws_credentials import SELECTION, StubSessions

ROLE_NAME = "ddak-ecs-infra-elb"
ROLE_PATH = "/ddak/infra/"
MANAGED_POLICY = "arn:aws:iam::aws:policy/AmazonECSInfrastructureRolePolicyForLoadBalancers"
TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Principal": {"Service": "ecs.amazonaws.com"},
        }
    ],
}
SETTINGS = replace(
    f.SETTINGS, layer="platform", outputs={}, state_bucket=f"ddak-fixture-{f.ACCOUNT}"
)


@pytest.fixture(autouse=True)
def prohibit_real_sdk_and_commands(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("이 테스트는 실제 SDK 세션이나 CLI 실행을 허용하지 않는다")

    monkeypatch.setattr("boto3.Session", forbidden)
    monkeypatch.setattr(f.CommandRunner, "run", forbidden)


def error(code, operation="GetRole"):
    return ClientError({"Error": {"Code": code, "Message": "fixture"}}, operation)


def matching_role():
    return {
        "Arn": f"arn:aws:iam::{f.ACCOUNT}:role{ROLE_PATH}{ROLE_NAME}",
        "RoleName": ROLE_NAME,
        "Path": ROLE_PATH,
        "AssumeRolePolicyDocument": deepcopy(TRUST),
    }


def sdk_clients(role=None):
    template = foundation.foundation_template(SETTINGS)
    s3 = Mock(
        spec=[
            "head_bucket",
            "get_bucket_tagging",
            "get_bucket_policy",
            "create_bucket",
            "put_bucket_tagging",
            "put_public_access_block",
            "put_bucket_policy",
            "put_bucket_versioning",
            "put_bucket_encryption",
        ]
    )
    s3.get_bucket_tagging.return_value = {
        "TagSet": [{"Key": k, "Value": v} for k, v in template["tags"].items()]
    }
    s3.get_bucket_policy.return_value = {"Policy": json.dumps(template["bucket_policy"])}
    iam = Mock(
        spec=[
            "get_policy",
            "get_policy_version",
            "get_role",
            "list_attached_role_policies",
            "list_role_policies",
            "create_policy",
            "create_role",
            "attach_role_policy",
            "create_policy_version",
        ]
    )
    iam.get_policy.side_effect = lambda **kw: {
        "Policy": {"Arn": kw["PolicyArn"], "DefaultVersionId": "v1"}
    }
    iam.list_attached_role_policies.return_value = {"AttachedPolicies": [], "IsTruncated": False}
    iam.list_role_policies.return_value = {"PolicyNames": [], "IsTruncated": False}
    documents = {
        SETTINGS.boundary_arn: template["boundary"],
        SETTINGS.build_boundary_arn: template["build_boundary"],
    }
    iam.get_policy_version.side_effect = lambda **kw: {
        "PolicyVersion": {
            "Document": documents[kw["PolicyArn"]],
            "VersionId": "v1",
            "IsDefaultVersion": True,
        }
    }
    if role is None:
        iam.get_role.side_effect = error("NoSuchEntity")
    else:
        iam.get_role.return_value = {"Role": deepcopy(role)}
    sts = Mock(spec=["get_caller_identity"])
    sts.get_caller_identity.return_value = {"Account": f.ACCOUNT}
    return {"s3": s3, "iam": iam, "sts": sts}


def apply(sdk, marker, records=None):
    template = foundation.foundation_template(SETTINGS)
    snapshots = [
        {
            "policy_arn": arn,
            "default_version_id": "v1",
            "document_sha256": f.digest(f.canonical(document)),
            "document": document,
        }
        for arn, document in (
            (SETTINGS.boundary_arn, template["boundary"]),
            (SETTINGS.build_boundary_arn, template["build_boundary"]),
        )
    ]
    if records is None:
        sha = foundation.foundation_approval_hash(SETTINGS, snapshots)
        records = [f.approval(sha, "foundation")]
    return foundation.apply_foundation(
        settings=SETTINGS,
        run_id="run-1",
        marker=marker,
        s3=sdk["s3"],
        iam=sdk["iam"],
        sts=sdk["sts"],
        approvals=lambda: records,
        guard=Mock(),
        expected_bucket_exists=True,
        expected_boundaries=snapshots,
    )


def assert_no_sdk_writes(sdk):
    for client in sdk.values():
        assert all(
            not call[0].startswith(("create_", "put_", "attach_", "delete_", "update_"))
            for call in client.mock_calls
        )


def runtime_plan(root):
    sdk, runner, records = sdk_clients(), f.FakeRunner(), []
    runner.raw = {
        "format_version": "1.2",
        "resource_changes": [f.resource("aws_ecs_cluster", "demo", ["create"], {"name": "demo"})],
    }
    runner.outputs = {}
    runtime = f.InfraRuntime(
        aws_project_settings=SELECTION,
        session_factory=StubSessions(),
        root=root,
        run_id="run-1",
        settings=SETTINGS,
        lock_file=b"fixture",
        approvals=lambda: records,
        guard=Mock(),
        runner=runner,
        foundation_clients=lambda session: sdk,
    )
    runtime.prepare_bootstrap(session=f.SESSION)
    assert runtime.validate(
        {"main.tf": 'resource "aws_ecs_cluster" "demo" { name = "demo" }\n'}
    ).passed
    summary = runtime.plan(session=f.SESSION, analyzer=Mock(), update=False)
    return runtime, runner, sdk, records, summary


def test_template_declares_fixed_ecs_role_and_managed_policy():
    assert foundation.foundation_template(SETTINGS)["ecs_infrastructure_role"] == {
        "name": ROLE_NAME,
        "path": ROLE_PATH,
        "arn": f"arn:aws:iam::{f.ACCOUNT}:role{ROLE_PATH}{ROLE_NAME}",
        "trust_policy": TRUST,
        "managed_policy_arn": MANAGED_POLICY,
    }


def test_new_role_is_created_with_ecs_trust_then_policy_attached(tmp_path):
    sdk, marker = sdk_clients(), tmp_path / "foundation-marker"
    result = apply(sdk, marker)
    sdk["iam"].create_role.assert_called_once_with(
        Path=ROLE_PATH, RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(TRUST)
    )
    sdk["iam"].attach_role_policy.assert_called_once_with(
        RoleName=ROLE_NAME, PolicyArn=MANAGED_POLICY
    )
    names = [call[0] for call in sdk["iam"].mock_calls]
    assert names.index("create_role") < names.index("attach_role_policy")
    sdk["iam"].list_attached_role_policies.assert_not_called()
    sdk["iam"].list_role_policies.assert_not_called()
    assert marker.read_text() == result["template_sha256"]


@pytest.mark.parametrize("already_attached", [False, True])
def test_matching_existing_role_is_reused_without_mutating_trust_or_boundary(
    tmp_path, already_attached
):
    sdk = sdk_clients(matching_role())
    if already_attached:
        sdk["iam"].list_attached_role_policies.return_value["AttachedPolicies"] = [
            {
                "PolicyName": "AmazonECSInfrastructureRolePolicyForLoadBalancers",
                "PolicyArn": MANAGED_POLICY,
            }
        ]
    apply(sdk, tmp_path / "foundation-marker")
    sdk["iam"].get_role.assert_called_once_with(RoleName=ROLE_NAME)
    sdk["iam"].list_attached_role_policies.assert_called_once_with(RoleName=ROLE_NAME)
    sdk["iam"].list_role_policies.assert_called_once_with(RoleName=ROLE_NAME)
    sdk["iam"].create_role.assert_not_called()
    sdk["iam"].create_policy.assert_not_called()
    sdk["iam"].attach_role_policy.assert_called_once_with(
        RoleName=ROLE_NAME, PolicyArn=MANAGED_POLICY
    )


POLICY_LISTS = [
    ("list_attached_role_policies", "AttachedPolicies"),
    ("list_role_policies", "PolicyNames"),
]


@pytest.mark.parametrize("operation,field", POLICY_LISTS)
@pytest.mark.parametrize("creation_race", [False, True])
def test_missing_truncation_flag_stops_reuse_and_creation_race(
    tmp_path, operation, field, creation_race
):
    sdk, marker = sdk_clients(matching_role()), tmp_path / "foundation-marker"
    if creation_race:
        sdk["iam"].get_role.side_effect = [
            error("NoSuchEntity"),
            {"Role": matching_role()},
        ]
        sdk["iam"].create_role.side_effect = error("EntityAlreadyExists", "CreateRole")
    listing = getattr(sdk["iam"], operation)
    listing.side_effect = [
        {field: [], "Marker": "next-page"},
        {field: ["fixture-unapproved"], "IsTruncated": False},
    ]
    with pytest.raises(DdakToolError) as exc:
        apply(sdk, marker)
    assert exc.value.code is ErrorCode.PRECONDITION_FAILED
    listing.assert_called_once_with(RoleName=ROLE_NAME)
    sdk["iam"].attach_role_policy.assert_not_called()
    assert marker.exists() is creation_race
    if not creation_race:
        assert_no_sdk_writes(sdk)


@pytest.mark.parametrize("operation,field", POLICY_LISTS)
@pytest.mark.parametrize("page_number", [1, 2])
def test_unapproved_policy_on_any_page_is_rejected_before_marker(
    tmp_path, operation, field, page_number
):
    sdk, marker = sdk_clients(matching_role()), tmp_path / "foundation-marker"
    extra = (
        [
            {
                "PolicyName": "fixture-unapproved",
                "PolicyArn": "arn:aws:iam::aws:policy/fixture-unapproved",
            }
        ]
        if field == "AttachedPolicies"
        else ["fixture-inline"]
    )
    pages = [{field: extra, "IsTruncated": False}]
    if page_number == 2:
        pages.insert(0, {field: [], "IsTruncated": True, "Marker": "next-page"})
    listing = getattr(sdk["iam"], operation)
    listing.side_effect = pages
    with pytest.raises(DdakToolError) as exc:
        apply(sdk, marker)
    assert exc.value.code is ErrorCode.PRECONDITION_FAILED
    expected_calls = [call(RoleName=ROLE_NAME)]
    if page_number == 2:
        expected_calls.append(call(RoleName=ROLE_NAME, Marker="next-page"))
    assert listing.call_args_list == expected_calls
    assert_no_sdk_writes(sdk)
    assert not marker.exists()


@pytest.mark.parametrize("operation,field", POLICY_LISTS)
def test_complete_policy_pagination_is_followed_before_reusing_role(tmp_path, operation, field):
    sdk = sdk_clients(matching_role())
    permitted = (
        [
            {
                "PolicyName": "AmazonECSInfrastructureRolePolicyForLoadBalancers",
                "PolicyArn": MANAGED_POLICY,
            }
        ]
        if field == "AttachedPolicies"
        else []
    )
    listing = getattr(sdk["iam"], operation)
    listing.side_effect = [
        {field: [], "IsTruncated": True, "Marker": "next-page"},
        {field: permitted, "IsTruncated": False},
    ]
    apply(sdk, tmp_path / "foundation-marker")
    assert listing.call_args_list == [
        call(RoleName=ROLE_NAME),
        call(RoleName=ROLE_NAME, Marker="next-page"),
    ]
    sdk["iam"].create_role.assert_not_called()
    sdk["iam"].attach_role_policy.assert_called_once_with(
        RoleName=ROLE_NAME, PolicyArn=MANAGED_POLICY
    )


@pytest.mark.parametrize("operation,field", POLICY_LISTS)
@pytest.mark.parametrize(
    "defect",
    [
        "missing-marker",
        "empty-marker",
        "non-string-marker",
        "repeated-marker",
        "non-bool-truncated",
        "missing-second-page-list",
    ],
)
def test_incomplete_policy_pagination_is_rejected_before_marker(tmp_path, operation, field, defect):
    sdk, marker = sdk_clients(matching_role()), tmp_path / "foundation-marker"
    first = {field: [], "IsTruncated": True}
    pages = [first]
    if defect == "empty-marker":
        first["Marker"] = ""
    elif defect == "non-string-marker":
        first["Marker"] = 1
    elif defect == "non-bool-truncated":
        first.update(IsTruncated="true", Marker="next-page")
    elif defect in {"repeated-marker", "missing-second-page-list"}:
        first["Marker"] = "next-page"
        pages.append(
            {field: [], "IsTruncated": True, "Marker": "next-page"}
            if defect == "repeated-marker"
            else {"IsTruncated": False}
        )
    listing = getattr(sdk["iam"], operation)
    listing.side_effect = pages
    with pytest.raises(DdakToolError) as exc:
        apply(sdk, marker)
    assert exc.value.code is ErrorCode.PRECONDITION_FAILED
    assert listing.call_count == len(pages)
    assert_no_sdk_writes(sdk)
    assert not marker.exists()


@pytest.mark.parametrize("operation,field", POLICY_LISTS)
def test_policy_listing_access_denied_stops_before_marker(tmp_path, operation, field):
    sdk, marker = sdk_clients(matching_role()), tmp_path / "foundation-marker"
    getattr(sdk["iam"], operation).side_effect = error("AccessDenied", operation)
    with pytest.raises(DdakToolError) as exc:
        apply(sdk, marker)
    assert exc.value.code is ErrorCode.ADAPTER_FAILED
    assert_no_sdk_writes(sdk)
    assert not marker.exists()


def mismatched_role(field):
    role = matching_role()
    if field == "path":
        role["Path"] = "/ddak/app/"
    elif field == "trust":
        role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] = {
            "Service": "ecs-tasks.amazonaws.com"
        }
    elif field == "boundary":
        role["PermissionsBoundary"] = {"PermissionsBoundaryArn": SETTINGS.boundary_arn}
    elif field == "arn":
        role["Arn"] = role["Arn"].replace(f.ACCOUNT, "999999999999")
    else:
        role["RoleName"] = "other-role"
    return role


@pytest.mark.parametrize("field", ["path", "trust", "boundary", "arn", "name"])
def test_mismatched_existing_role_stops_before_any_sdk_write(tmp_path, field):
    sdk, marker = sdk_clients(mismatched_role(field)), tmp_path / "foundation-marker"
    with pytest.raises(DdakToolError) as exc:
        apply(sdk, marker)
    assert exc.value.code is ErrorCode.PRECONDITION_FAILED
    assert_no_sdk_writes(sdk)
    assert not marker.exists()


@pytest.mark.parametrize("field", [None, "path", "trust", "boundary", "missing"])
def test_create_role_race_revalidates_before_policy_attachment(tmp_path, field):
    sdk, marker = sdk_clients(), tmp_path / "foundation-marker"
    raced = (
        error("NoSuchEntity")
        if field == "missing"
        else {"Role": matching_role() if field is None else mismatched_role(field)}
    )
    sdk["iam"].get_role.side_effect = [error("NoSuchEntity"), raced]
    sdk["iam"].create_role.side_effect = error("EntityAlreadyExists", "CreateRole")
    if field is None:
        apply(sdk, marker)
        sdk["iam"].attach_role_policy.assert_called_once_with(
            RoleName=ROLE_NAME, PolicyArn=MANAGED_POLICY
        )
    else:
        with pytest.raises(DdakToolError) as exc:
            apply(sdk, marker)
        assert exc.value.code is ErrorCode.PRECONDITION_FAILED
        sdk["iam"].attach_role_policy.assert_not_called()
    assert sdk["iam"].get_role.call_count == 2
    assert marker.exists()


@pytest.mark.parametrize("operation", ["create_role", "attach_role_policy"])
def test_access_denied_preserves_marker_and_blocks_same_run_retry(tmp_path, operation):
    sdk, marker = sdk_clients(), tmp_path / "foundation-marker"
    getattr(sdk["iam"], operation).side_effect = error("AccessDenied", operation)
    with pytest.raises(DdakToolError) as exc:
        apply(sdk, marker)
    assert exc.value.code is ErrorCode.ADAPTER_FAILED
    expected = f.digest(f.canonical(foundation.foundation_template(SETTINGS)))
    assert marker.read_text() == expected
    if operation == "create_role":
        sdk["iam"].attach_role_policy.assert_not_called()
    before = {name: len(client.mock_calls) for name, client in sdk.items()}
    with pytest.raises(DdakToolError) as retry:
        apply(sdk, marker)
    assert retry.value.code is ErrorCode.PRECONDITION_FAILED
    assert {name: len(client.mock_calls) for name, client in sdk.items()} == before
    assert marker.read_text() == expected


@pytest.mark.parametrize("decision", ["missing", "denied", "wrong-hash"])
def test_foundation_without_matching_approval_makes_no_sdk_calls(tmp_path, decision):
    sdk, marker = sdk_clients(), tmp_path / "foundation-marker"
    sha = f.digest(f.canonical(foundation.foundation_template(SETTINGS)))
    records = (
        []
        if decision == "missing"
        else [
            f.approval(
                f.digest(b"different-template") if decision == "wrong-hash" else sha,
                "foundation",
                decision="denied" if decision == "denied" else "approved",
            )
        ]
    )
    with pytest.raises(DdakToolError) as exc:
        apply(sdk, marker, records)
    assert exc.value.code is ErrorCode.APPROVAL_REQUIRED
    assert all(not client.mock_calls for client in sdk.values())
    assert not marker.exists()


def test_infra_plan_and_unapproved_apply_do_not_write_to_sdk(tmp_path):
    runtime, runner, sdk, _records, _summary = runtime_plan(tmp_path)
    assert_no_sdk_writes(sdk)
    with pytest.raises(DdakToolError) as exc:
        runtime.apply(session=f.SESSION)
    assert exc.value.code is ErrorCode.APPROVAL_REQUIRED
    assert_no_sdk_writes(sdk)
    assert not any(argv[1] == "apply" for argv, _ in runner.calls)


def test_changed_role_template_changes_infra_hash_and_requires_new_approval(tmp_path, monkeypatch):
    old, runner, sdk, records, old_summary = runtime_plan(tmp_path / "old")
    records.append(f.approval(old_summary["plan_sha256"]))
    changed = deepcopy(foundation.foundation_template(SETTINGS))
    changed["ecs_infrastructure_role"]["trust_policy"]["Statement"][0]["Condition"] = {
        "StringEquals": {"aws:SourceAccount": f.ACCOUNT}
    }
    assert f.digest(f.canonical(changed)) != f.digest(old._foundation)
    monkeypatch.setattr(foundation, "foundation_template", lambda settings: deepcopy(changed))
    with pytest.raises(DdakToolError) as exc:
        old.apply(session=f.SESSION)
    assert exc.value.code is ErrorCode.APPROVAL_REQUIRED
    assert_no_sdk_writes(sdk)
    assert not any(argv[1] == "apply" for argv, _ in runner.calls)

    new, _runner, new_sdk, new_records, new_summary = runtime_plan(tmp_path / "new")
    assert new_summary["plan_sha256"] != old_summary["plan_sha256"]
    new_records.append(f.approval(old_summary["plan_sha256"]))
    with pytest.raises(DdakToolError) as exc:
        new.apply(session=f.SESSION)
    assert exc.value.code is ErrorCode.APPROVAL_REQUIRED
    assert_no_sdk_writes(new_sdk)
    new_records.append(f.approval(new_summary["plan_sha256"]))
    result = new.apply(session=f.SESSION)
    assert result["plan_sha256"] == new_summary["plan_sha256"]
    assert (new.work / "apply-succeeded").exists()
    new_sdk["iam"].create_role.assert_called_once_with(
        Path=ROLE_PATH,
        RoleName=ROLE_NAME,
        AssumeRolePolicyDocument=json.dumps(changed["ecs_infrastructure_role"]["trust_policy"]),
    )


def test_approval_metadata_preserves_role_information_and_masks_account(tmp_path):
    _runtime, _runner, sdk, _records, summary = runtime_plan(tmp_path)
    encoded = encode_meta(summary, infra=True)
    decoded = json.loads(encoded)
    row = next(item for item in decoded["iam_diff"] if item["address"] == "ddak.foundation")
    role = row["ecs_infrastructure_role"]
    assert role == {
        "arn": f"arn:aws:iam::{'*' * 12}:role{ROLE_PATH}{ROLE_NAME}",
        "trust_policy": TRUST["Statement"],
        "managed_policy_arn": MANAGED_POLICY,
    }
    assert row == next(item for item in summary["iam_diff"] if item["address"] == "ddak.foundation")
    assert decoded["plan_sha256"] == summary["plan_sha256"]
    assert f.ACCOUNT not in encoded
    assert f.ACCOUNT not in json.dumps(summary)
    assert "*" * 12 in row["bucket"]
    assert_no_sdk_writes(sdk)


@pytest.mark.parametrize("project", ["flaskr", "inventory-api"])
def test_sdk_and_reference_hcl_app_log_scope_match_narrow_ecs_prefix(project):
    source = Path(foundation.__file__).parent / "terraform" / "foundation" / "main.tf"
    parsed = loads(source.read_text())
    app = next(
        item["aws_iam_policy"]["app_boundary"]
        for item in parsed["resource"]
        if "app_boundary" in item.get("aws_iam_policy", {})
    )
    hcl_document = policy_json(app["policy"])
    sdk_document = boundary_document(f.ACCOUNT, project)
    expected = {
        "Effect": "Allow",
        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": [
            f"arn:aws:logs:ap-northeast-2:{f.ACCOUNT}:log-group:/aws/ecs/{project}:*",
            f"arn:aws:logs:ap-northeast-2:{f.ACCOUNT}:log-group:/aws/ecs/ddak-*:*",
        ],
    }
    for document in (hcl_document, sdk_document):
        logs = [
            row
            for row in document["Statement"]
            if any(
                action.startswith("logs:")
                for action in ([row["Action"]] if isinstance(row["Action"], str) else row["Action"])
            )
        ]
        assert len(logs) == 1
        normalized = dict(logs[0])
        assert isinstance(normalized["Resource"], list)
        normalized["Resource"] = [
            ref.replace("${var.account_id}", f.ACCOUNT).replace("${var.project}", project)
            for ref in normalized["Resource"]
        ]
        assert normalized == expected


@pytest.mark.parametrize("project", ["flaskr", "inventory-api"])
def test_foundation_template_derives_log_scope_from_settings_project(project):
    template = foundation.foundation_template(replace(SETTINGS, project=project))
    logs = [
        row for row in template["boundary"]["Statement"] if "logs:PutLogEvents" in row["Action"]
    ]
    assert len(logs) == 1
    assert logs[0]["Resource"] == [
        f"arn:aws:logs:ap-northeast-2:{f.ACCOUNT}:log-group:/aws/ecs/{project}:*",
        f"arn:aws:logs:ap-northeast-2:{f.ACCOUNT}:log-group:/aws/ecs/ddak-*:*",
    ]


@pytest.mark.parametrize("boundary", ["boundary", "build_boundary"])
def test_unapproved_boundary_drift_is_rejected_without_creating_policy_version(tmp_path, boundary):
    sdk, marker = sdk_clients(), tmp_path / "foundation-marker"
    template = foundation.foundation_template(SETTINGS)
    documents = {
        SETTINGS.boundary_arn: deepcopy(template["boundary"]),
        SETTINGS.build_boundary_arn: deepcopy(template["build_boundary"]),
    }
    arn = SETTINGS.boundary_arn if boundary == "boundary" else SETTINGS.build_boundary_arn
    documents[arn]["Statement"][0]["Resource"] = "arn:aws:iam::aws:policy/fixture-different"
    sdk["iam"].get_policy_version.side_effect = lambda **kw: {
        "PolicyVersion": {"Document": documents[kw["PolicyArn"]]}
    }
    with pytest.raises(DdakToolError) as exc:
        apply(sdk, marker)
    assert exc.value.code is ErrorCode.PRECONDITION_FAILED
    sdk["iam"].create_policy_version.assert_not_called()
    sdk["iam"].create_policy.assert_not_called()
    sdk["iam"].create_role.assert_not_called()
    sdk["iam"].attach_role_policy.assert_not_called()
    assert_no_sdk_writes(sdk)
    assert not marker.exists()
