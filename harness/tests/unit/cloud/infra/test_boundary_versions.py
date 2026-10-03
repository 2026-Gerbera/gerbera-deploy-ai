"""권한 경계 버전 관리: 명시적 가짜 자격증명과 Stubber만 사용한다."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import Mock
from urllib.parse import quote

import boto3
import pytest
from botocore.exceptions import ReadTimeoutError
from botocore.stub import Stubber

from ddak.cloud.infra.boundary_versions import (
    apply_boundaries,
    boundary_changes,
    check_boundaries,
    snapshot_boundaries,
)
from ddak.cloud.infra.providers.aws import (
    BOUNDARY_PATH,
    boundary_document,
    build_boundary_document,
)
from ddak.cloud.infra.runtime import AwsSettings, canonical, digest
from ddak.core.contracts.errors import DdakToolError, ErrorCode

ACCOUNT = "123456789012"
SETTINGS = AwsSettings("demo", ACCOUNT, "ddak-unit-state", "platform", {})
ARNS = [SETTINGS.boundary_arn, SETTINGS.build_boundary_arn]
DOCUMENTS = [boundary_document(ACCOUNT, "demo"), build_boundary_document(ACCOUNT)]
NOW = datetime(2026, 10, 3, tzinfo=UTC)


@pytest.fixture
def sdk():
    iam = boto3.client(
        "iam",
        region_name="ap-northeast-2",
        aws_access_key_id="unit-access",
        aws_secret_access_key="unit-secret",
    )
    with Stubber(iam) as stub:
        yield iam, stub
        stub.assert_no_pending_responses()


def policy(arn, version="v1"):
    return {
        "Arn": arn,
        "PolicyName": arn.rsplit("/", 1)[1],
        "Path": BOUNDARY_PATH,
        "DefaultVersionId": version,
        "PolicyId": "ANPAUNIT0000000000000",
        "CreateDate": NOW,
        "UpdateDate": NOW,
    }


def observed(index, document=None, version="v1"):
    doc = deepcopy(DOCUMENTS[index] if document is None else document)
    return {
        "policy_arn": ARNS[index],
        "default_version_id": version,
        "document_sha256": digest(canonical(doc)),
        "document": doc,
    }


def missing(index):
    return {
        "policy_arn": ARNS[index],
        "default_version_id": None,
        "document_sha256": None,
        "document": None,
    }


def old_document():
    return {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": "logs:CreateLogStream", "Resource": "*"}],
    }


def queue_read(stub, item, *, encoded=False):
    arn = item["policy_arn"]
    if item["document"] is None:
        stub.add_client_error("get_policy", "NoSuchEntity", expected_params={"PolicyArn": arn})
        return
    stub.add_response(
        "get_policy",
        {"Policy": policy(arn, item["default_version_id"])},
        {"PolicyArn": arn},
    )
    document = item["document"]
    stub.add_response(
        "get_policy_version",
        {
            "PolicyVersion": {
                "Document": (
                    quote(canonical(document).decode(), safe="")
                    if encoded
                    else canonical(document).decode()
                ),
                "VersionId": item["default_version_id"],
                "IsDefaultVersion": True,
            }
        },
        {"PolicyArn": arn, "VersionId": item["default_version_id"]},
    )


def versions(*ids, default="v1"):
    return [{"VersionId": value, "IsDefaultVersion": value == default} for value in ids]


def queue_versions(stub, items, *, marker=None, truncated=False, next_marker=None):
    request = {"PolicyArn": ARNS[0]}
    if marker is not None:
        request["Marker"] = marker
    response = {"Versions": items, "IsTruncated": truncated}
    if next_marker is not None:
        response["Marker"] = next_marker
    stub.add_response("list_policy_versions", response, request)


def test_snapshot_normalizes_document_and_supports_official_version_ids(sdk):
    iam, stub = sdk
    document = old_document()
    document["Statement"][0]["Sid"] = "문서+plus%2B"
    expected = [observed(0, document, "v2.test-"), observed(1, version="v3.")]
    for item in expected:
        queue_read(stub, item, encoded=True)
    assert snapshot_boundaries(SETTINGS, iam) == expected


def test_absent_snapshots_have_only_none_values(sdk):
    iam, stub = sdk
    expected = [missing(0), missing(1)]
    for item in expected:
        queue_read(stub, item)
    assert snapshot_boundaries(SETTINGS, iam) == expected


def test_missing_default_version_is_not_policy_absence(sdk):
    iam, stub = sdk
    stub.add_response("get_policy", {"Policy": policy(ARNS[0])}, {"PolicyArn": ARNS[0]})
    stub.add_client_error(
        "get_policy_version",
        "NoSuchEntity",
        expected_params={"PolicyArn": ARNS[0], "VersionId": "v1"},
    )
    with pytest.raises(DdakToolError) as error:
        snapshot_boundaries(SETTINGS, iam)
    assert error.value.code == ErrorCode.PRECONDITION_FAILED


@pytest.mark.parametrize("field,value", [("Arn", ARNS[1]), ("DefaultVersionId", "v0")])
def test_bad_policy_response_stops_before_version_read(sdk, field, value):
    iam, stub = sdk
    response = policy(ARNS[0])
    response[field] = value
    stub.add_response("get_policy", {"Policy": response}, {"PolicyArn": ARNS[0]})
    with pytest.raises(DdakToolError):
        snapshot_boundaries(SETTINGS, iam)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"VersionId": "v1", "IsDefaultVersion": True},
        {"Document": "invalid-json", "VersionId": "v1", "IsDefaultVersion": True},
        {"Document": "{}", "VersionId": "v2", "IsDefaultVersion": True},
        {"Document": "{}", "VersionId": "v1", "IsDefaultVersion": False},
    ],
)
def test_missing_or_wrong_version_response_is_explicit_failure(sdk, response):
    iam, stub = sdk
    stub.add_response("get_policy", {"Policy": policy(ARNS[0])}, {"PolicyArn": ARNS[0]})
    stub.add_response(
        "get_policy_version",
        {"PolicyVersion": response},
        {"PolicyArn": ARNS[0], "VersionId": "v1"},
    )
    with pytest.raises(DdakToolError) as error:
        snapshot_boundaries(SETTINGS, iam)
    assert ACCOUNT not in str(error.value)


@pytest.mark.parametrize("operation", [boundary_changes, check_boundaries, apply_boundaries])
def test_other_input_arn_rejected_without_any_iam_call(sdk, operation):
    iam, _ = sdk
    snapshots = [observed(0), observed(1)]
    snapshots[1]["policy_arn"] = f"arn:aws:iam::{ACCOUNT}:policy/other"
    with pytest.raises(DdakToolError):
        if operation is boundary_changes:
            operation(SETTINGS, snapshots)
        elif operation is apply_boundaries:
            operation(SETTINGS, iam, snapshots, guard=Mock(), record=Mock())
        else:
            operation(SETTINGS, iam, snapshots)


def test_settings_cannot_redirect_a_boundary_read(sdk):
    iam, _ = sdk

    class RedirectedSettings(AwsSettings):
        @property
        def build_boundary_arn(self):
            return f"arn:aws:iam::{self.account_id}:policy/other"

    settings = RedirectedSettings("demo", ACCOUNT, "ddak-unit-state", "platform", {})
    with pytest.raises(DdakToolError):
        snapshot_boundaries(settings, iam)


def test_changes_include_full_statement_deltas_and_mask_every_nested_account():
    old = deepcopy(DOCUMENTS[0])
    old["Statement"][0]["Condition"] = {"StringEquals": {ACCOUNT: ["987654321098", 987654321098]}}
    changes = boundary_changes(SETTINGS, [observed(0, old), missing(1)])
    change = changes[0]
    assert change["action"] == "update"
    assert change["previous_document_sha256"] == digest(canonical(old))
    assert change["template_sha256"] == digest(canonical(DOCUMENTS[0]))
    assert change["added_statements"] == [
        {
            **DOCUMENTS[0]["Statement"][0],
            "Resource": DOCUMENTS[0]["Statement"][0]["Resource"].replace(ACCOUNT, "*" * 12),
        }
    ]
    assert change["removed_statements"][0]["Condition"] == {
        "StringEquals": {"*" * 12: ["*" * 12, "*" * 12]}
    }
    assert change["added_resources"] == change["removed_resources"] == []
    assert changes[1]["action"] == "create"
    assert changes[1]["removed_statements"] == []
    assert ACCOUNT.encode() not in canonical(changes)
    assert b"987654321098" not in canonical(changes)
    assert len(canonical(changes)) <= 8192


def test_statement_order_is_ignored_for_delta_but_hash_remains_canonical():
    old = deepcopy(DOCUMENTS[0])
    old["Statement"].reverse()
    change = boundary_changes(SETTINGS, [observed(0, old), observed(1)])[0]
    assert change["action"] == "update"
    assert change["previous_document_sha256"] != change["template_sha256"]
    assert change["added_statements"] == change["removed_statements"] == []


def test_unchanged_has_no_list_call_or_mutation(sdk):
    iam, stub = sdk
    snapshots = [observed(0), observed(1)]
    for item in snapshots:
        queue_read(stub, item)
    check_boundaries(SETTINGS, iam, snapshots)
    for item in snapshots:
        queue_read(stub, item)
    record, guard = Mock(), Mock()
    result = apply_boundaries(SETTINGS, iam, snapshots, guard=guard, record=record)
    assert [item["status"] for item in result] == ["unchanged", "unchanged"]
    assert record.call_count == 2
    guard.assert_not_called()


@pytest.mark.parametrize("drift", ["version", "sha", "different_to_same", "absent"])
@pytest.mark.parametrize("apply", [False, True])
def test_approval_drift_precedes_noop_or_version_listing(sdk, drift, apply):
    iam, stub = sdk
    before = observed(0, old_document())
    current = deepcopy(before)
    if drift == "version":
        current["default_version_id"] = "v2"
    elif drift == "sha":
        current = observed(0, {**old_document(), "Id": "changed"})
    elif drift == "different_to_same":
        current = observed(0)
    else:
        current = missing(0)
    queue_read(stub, current)
    with pytest.raises(DdakToolError) as error:
        if apply:
            apply_boundaries(SETTINGS, iam, [before, observed(1)], guard=Mock(), record=Mock())
        else:
            check_boundaries(SETTINGS, iam, [before, observed(1)])
    assert error.value.code == ErrorCode.PRECONDITION_FAILED


def test_check_reads_all_version_pages_below_limit(sdk):
    iam, stub = sdk
    snapshots = [observed(0, old_document()), observed(1)]
    queue_read(stub, snapshots[0])
    queue_versions(stub, versions("v1", "v2"), truncated=True, next_marker="next")
    queue_versions(stub, versions("v3", "v4"), marker="next")
    queue_read(stub, snapshots[1])
    check_boundaries(SETTINGS, iam, snapshots)


@pytest.mark.parametrize("apply", [False, True])
def test_five_versions_are_counted_across_pages_and_never_deleted(sdk, apply):
    iam, stub = sdk
    snapshots = [observed(0, old_document()), observed(1)]
    queue_read(stub, snapshots[0])
    queue_versions(stub, versions("v1", "v2", "v3"), truncated=True, next_marker="next")
    queue_versions(stub, versions("v4", "v5"), marker="next")
    with pytest.raises(DdakToolError, match=r"5.*삭제"):
        if apply:
            apply_boundaries(SETTINGS, iam, snapshots, guard=Mock(), record=Mock())
        else:
            check_boundaries(SETTINGS, iam, snapshots)


@pytest.mark.parametrize(
    "response",
    [
        {"Versions": versions("v1")},
        {"Versions": versions("v1"), "IsTruncated": True},
        {"Versions": versions("v1", "v1"), "IsTruncated": False},
        {"Versions": [], "IsTruncated": False},
        {"Versions": versions("v2"), "IsTruncated": False},
        {"Versions": [{"VersionId": "v1"}], "IsTruncated": False},
        {"Versions": [{"VersionId": "v0", "IsDefaultVersion": True}], "IsTruncated": False},
        {"Versions": versions("v1"), "IsTruncated": False, "Marker": "unexpected"},
    ],
)
def test_incomplete_or_manipulated_version_list_fails(sdk, response):
    iam, stub = sdk
    snapshots = [observed(0, old_document()), observed(1)]
    queue_read(stub, snapshots[0])
    stub.add_response("list_policy_versions", response, {"PolicyArn": ARNS[0]})
    with pytest.raises(DdakToolError):
        check_boundaries(SETTINGS, iam, snapshots)


def test_repeated_marker_is_rejected(sdk):
    iam, stub = sdk
    snapshots = [observed(0, old_document()), observed(1)]
    queue_read(stub, snapshots[0])
    queue_versions(stub, versions("v1"), truncated=True, next_marker="repeat")
    queue_versions(stub, versions("v2"), marker="repeat", truncated=True, next_marker="repeat")
    with pytest.raises(DdakToolError):
        check_boundaries(SETTINGS, iam, snapshots)


def test_create_records_unknown_before_api_and_actual_immediately_after(sdk):
    iam, stub = sdk
    snapshots = [missing(0), missing(1)]
    events = []
    for index, item in enumerate(snapshots):
        queue_read(stub, item)
        stub.add_response(
            "create_policy",
            {"Policy": policy(ARNS[index])},
            {
                "PolicyName": ARNS[index].rsplit("/", 1)[1],
                "Path": BOUNDARY_PATH,
                "PolicyDocument": canonical(DOCUMENTS[index]).decode(),
                "Tags": [
                    {"Key": "ManagedBy", "Value": "ddak"},
                    {"Key": "Project", "Value": "demo"},
                ],
            },
        )
    original = iam.create_policy

    def create(**kwargs):
        assert events[-1] == "unknown"
        events.append("api")
        return original(**kwargs)

    iam.create_policy = create
    result = apply_boundaries(
        SETTINGS,
        iam,
        snapshots,
        guard=lambda: events.append("guard"),
        record=lambda row: events.append(row["status"]),
    )
    assert events == ["guard", "unknown", "api", "created"] * 2
    assert all(row["new_version_id"] == "v1" for row in result)
    assert all(row["previous_version_id"] is None for row in result)
    assert ACCOUNT.encode() not in canonical(result)


def test_update_sets_default_in_single_api_and_records_previous_and_new(sdk):
    iam, stub = sdk
    snapshots = [observed(0, old_document()), observed(1)]
    queue_read(stub, snapshots[0])
    queue_versions(stub, versions("v1", "v2", "v3", "v4"))
    stub.add_response(
        "create_policy_version",
        {"PolicyVersion": {"VersionId": "v5", "IsDefaultVersion": True}},
        {
            "PolicyArn": ARNS[0],
            "PolicyDocument": canonical(DOCUMENTS[0]).decode(),
            "SetAsDefault": True,
        },
    )
    queue_read(stub, snapshots[1])
    rows = []
    result = apply_boundaries(SETTINGS, iam, snapshots, guard=Mock(), record=rows.append)
    assert [row["status"] for row in rows] == ["unknown", "updated", "unchanged"]
    assert rows[0]["new_version_id"] is None
    assert result[0]["previous_version_id"] == "v1"
    assert result[0]["new_version_id"] == "v5"


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"VersionId": "v2"},
        {"VersionId": "v2", "IsDefaultVersion": False},
        {"VersionId": "v1", "IsDefaultVersion": True},
    ],
)
def test_ambiguous_update_response_keeps_unknown_and_requires_human(sdk, response):
    iam, stub = sdk
    snapshots = [observed(0, old_document()), observed(1)]
    queue_read(stub, snapshots[0])
    queue_versions(stub, versions("v1"))
    stub.add_response(
        "create_policy_version",
        {"PolicyVersion": response},
        {
            "PolicyArn": ARNS[0],
            "PolicyDocument": canonical(DOCUMENTS[0]).decode(),
            "SetAsDefault": True,
        },
    )
    rows = []
    with pytest.raises(DdakToolError) as error:
        apply_boundaries(SETTINGS, iam, snapshots, guard=Mock(), record=rows.append)
    assert error.value.needs_human
    assert [row["status"] for row in rows] == ["unknown"]


def test_api_response_loss_preserves_unknown_and_sanitizes_error(sdk):
    iam, stub = sdk
    queue_read(stub, missing(0))
    rows = []
    iam.create_policy = Mock(side_effect=ReadTimeoutError(endpoint_url=f"https://{ACCOUNT}"))
    with pytest.raises(DdakToolError) as error:
        apply_boundaries(SETTINGS, iam, [missing(0), missing(1)], guard=Mock(), record=rows.append)
    assert error.value.needs_human
    assert error.value.code == ErrorCode.ADAPTER_FAILED
    assert [row["status"] for row in rows] == ["unknown"]
    assert ACCOUNT not in str(error.value)


def test_second_policy_drift_preserves_first_mutation_record(sdk):
    iam, stub = sdk
    queue_read(stub, missing(0))
    stub.add_response(
        "create_policy",
        {"Policy": policy(ARNS[0])},
        {
            "PolicyName": "ddak-app-boundary",
            "Path": BOUNDARY_PATH,
            "PolicyDocument": canonical(DOCUMENTS[0]).decode(),
            "Tags": [{"Key": "ManagedBy", "Value": "ddak"}, {"Key": "Project", "Value": "demo"}],
        },
    )
    queue_read(stub, observed(1))
    rows = []
    with pytest.raises(DdakToolError) as error:
        apply_boundaries(SETTINGS, iam, [missing(0), missing(1)], guard=Mock(), record=rows.append)
    assert error.value.code == ErrorCode.PRECONDITION_FAILED
    assert error.value.needs_human
    assert [row["status"] for row in rows] == ["unknown", "created"]


def test_guard_failure_and_failed_unknown_record_prevent_mutation(sdk):
    iam, stub = sdk
    queue_read(stub, missing(0))
    record = Mock()
    guard = Mock(side_effect=DdakToolError(ErrorCode.LOCK_INVALID, "잠금 상실"))
    with pytest.raises(DdakToolError):
        apply_boundaries(SETTINGS, iam, [missing(0), missing(1)], guard=guard, record=record)
    record.assert_not_called()
    queue_read(stub, missing(0))
    with pytest.raises(DdakToolError):
        apply_boundaries(
            SETTINGS,
            iam,
            [missing(0), missing(1)],
            guard=Mock(),
            record=Mock(side_effect=OSError("기록 실패")),
        )


def test_tampered_snapshot_hash_is_rejected_before_iam(sdk):
    iam, _ = sdk
    snapshots = [observed(0), observed(1)]
    snapshots[0]["document_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(DdakToolError):
        check_boundaries(SETTINGS, iam, snapshots)


def test_snapshot_accepts_sdk_decoded_document_dict_and_preserves_raw_percent(sdk):
    iam, stub = sdk
    document = old_document()
    document["Statement"][0]["Sid"] = "raw+%2B"
    expected = [observed(0, document), observed(1)]
    for item in expected:
        queue_read(stub, item, encoded=True)
    original = iam.get_policy_version

    def decoded(**kwargs):
        response = original(**kwargs)
        document = response["PolicyVersion"]["Document"]
        if isinstance(document, str):
            response["PolicyVersion"]["Document"] = json.loads(document)
        return response

    iam.get_policy_version = decoded
    assert snapshot_boundaries(SETTINGS, iam) == expected


def test_duplicate_version_id_across_pages_fails(sdk):
    iam, stub = sdk
    snapshots = [observed(0, old_document()), observed(1)]
    queue_read(stub, snapshots[0])
    queue_versions(stub, versions("v1", "v2"), truncated=True, next_marker="next")
    queue_versions(stub, versions("v2", "v3"), marker="next")
    with pytest.raises(DdakToolError):
        check_boundaries(SETTINGS, iam, snapshots)


def test_other_arn_in_version_response_is_rejected_without_following_it(sdk):
    iam, stub = sdk
    queue_read(stub, observed(0))
    original = iam.get_policy_version

    def redirected(**kwargs):
        response = original(**kwargs)
        response["PolicyVersion"]["PolicyArn"] = ARNS[1]
        return response

    iam.get_policy_version = redirected
    with pytest.raises(DdakToolError):
        snapshot_boundaries(SETTINGS, iam)


def test_changes_reuse_rds_policy_view_and_keep_all_statement_fields():
    old = deepcopy(DOCUMENTS[0])
    old["Statement"][0]["Resource"] = (
        f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:rds!db-specific"
    )
    old["Statement"][0]["Sid"] = "KeepThisField"
    changes = boundary_changes(SETTINGS, [observed(0, old), missing(1)])
    statement = changes[0]["removed_statements"][0]
    assert statement["Sid"] == "KeepThisField"
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "secretsmanager:GetSecretValue"
    assert statement["Resource"].startswith("RDS master ARN sha256 ")
    assert b":secret:rds!" not in canonical(changes)
    created = boundary_changes(SETTINGS, [missing(0), missing(1)])
    assert b"RDS bootstrap" in canonical(created)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"Arn": ARNS[1], "DefaultVersionId": "v1"},
        {"Arn": ARNS[0]},
        {"Arn": ARNS[0], "DefaultVersionId": "v0"},
    ],
)
def test_missing_or_wrong_create_result_keeps_unknown(sdk, response):
    iam, stub = sdk
    queue_read(stub, missing(0))
    stub.add_response(
        "create_policy",
        {"Policy": response},
        {
            "PolicyName": "ddak-app-boundary",
            "Path": BOUNDARY_PATH,
            "PolicyDocument": canonical(DOCUMENTS[0]).decode(),
            "Tags": [{"Key": "ManagedBy", "Value": "ddak"}, {"Key": "Project", "Value": "demo"}],
        },
    )
    rows = []
    with pytest.raises(DdakToolError) as error:
        apply_boundaries(SETTINGS, iam, [missing(0), observed(1)], guard=Mock(), record=rows.append)
    assert error.value.needs_human
    assert [row["status"] for row in rows] == ["unknown"]


def test_create_only_requires_arn_and_default_version_and_callbacks_get_copies(sdk):
    iam, stub = sdk
    queue_read(stub, missing(0))
    stub.add_response(
        "create_policy",
        {"Policy": {"Arn": ARNS[0], "DefaultVersionId": "v1"}},
        {
            "PolicyName": "ddak-app-boundary",
            "Path": BOUNDARY_PATH,
            "PolicyDocument": canonical(DOCUMENTS[0]).decode(),
            "Tags": [{"Key": "ManagedBy", "Value": "ddak"}, {"Key": "Project", "Value": "demo"}],
        },
    )
    queue_read(stub, observed(1))
    rows = []
    result = apply_boundaries(
        SETTINGS, iam, [missing(0), observed(1)], guard=Mock(), record=rows.append
    )
    assert rows[0]["status"] == "unknown"
    assert rows[0]["new_version_id"] is None
    assert result[0]["status"] == "created"


def test_missing_pagination_flag_after_first_page_fails(sdk):
    iam, stub = sdk
    snapshots = [observed(0, old_document()), observed(1)]
    queue_read(stub, snapshots[0])
    queue_versions(stub, versions("v1"), truncated=True, next_marker="next")
    stub.add_response(
        "list_policy_versions",
        {"Versions": versions("v2")},
        {"PolicyArn": ARNS[0], "Marker": "next"},
    )
    with pytest.raises(DdakToolError):
        check_boundaries(SETTINGS, iam, snapshots)


def test_raw_json_snapshot_does_not_url_decode_literal_percent_sequences(sdk):
    iam, stub = sdk
    document = old_document()
    document["Statement"][0]["Sid"] = "literal%2B+"
    expected = [observed(0, document), observed(1)]
    for item in expected:
        # IAM 전송 형식은 URL 인코딩이며 botocore가 한 번 해제한다.
        queue_read(stub, item, encoded=True)
    original = iam.get_policy_version

    def raw_json(**kwargs):
        response = original(**kwargs)
        document = response["PolicyVersion"]["Document"]
        if isinstance(document, dict):
            response["PolicyVersion"]["Document"] = json.dumps(document)
        return response

    iam.get_policy_version = raw_json
    assert snapshot_boundaries(SETTINGS, iam) == expected
