"""source=fixture: 저장소 UPDATE의 단일 승인과 경계 버전·출력 연결."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from ddak.cloud.infra import bind_infra, fixture_binding, run_plan, run_validate, unbind_infra
from ddak.cloud.infra.runtime import AwsSettings, CommandRunner
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.tools.plan_infra import PlanInfraInput
from ddak.core.contracts.tools.validate_infra import ValidateInfraInput
from ddak.core.storage import bucket_name
from ddak.executor.approval_meta import encode_meta
from ddak.executor.infra import refresh_infra_context
from tests.unit.cloud.infra.test_runtime import approval
from tests.unit.test_deployment_service import step

ACCOUNT = "123456789012"
BUCKET = bucket_name("flaskr", 1)


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("실제 외부 호출 금지")

    monkeypatch.setattr("boto3.Session", forbidden)
    monkeypatch.setattr(CommandRunner, "run", forbidden)


def context(intent="create", *, bucket=BUCKET, **kwargs):
    return RunContext(
        "run-1",
        project="flaskr",
        mode=RunMode.UPDATE,
        adapter_mode=AdapterMode.FAKE,
        targets="cloud",
        platform={
            "cloud": {
                "task_role_arn": f"arn:aws:iam::{ACCOUNT}:role/ddak/app/flaskr-task",
                **({"upload_bucket": bucket} if intent == "remove" else {}),
            }
        },
        project_settings={
            "_infra_storage": {
                "intent": intent,
                "bucket": bucket,
                "evidence": [{"file": "flaskr/uploads.py", "line": 7, "kind": "hardcoded_dir"}]
                if intent == "create"
                else [],
            }
        },
        **kwargs,
    )


def setup(tmp_path, intent="create", *, bucket=BUCKET, sdk=None):
    ctx = context(intent, bucket=bucket)
    records = []
    binding = fixture_binding(ctx, root=tmp_path, approvals=lambda: records, guard=Mock())
    if sdk is not None:
        binding.runtime.foundation_clients = lambda _: {
            service: sdk for service in ("s3", "iam", "sts")
        }
    bind_infra(binding)
    try:
        assert run_validate(ValidateInfraInput(run_id=ctx.run_id), ctx).passed
        output = run_plan(PlanInfraInput(run_id=ctx.run_id), ctx)
    finally:
        unbind_infra(ctx.run_id)
    return ctx, binding, records, output


@pytest.mark.parametrize("intent", ["create", "remove"])
@pytest.mark.parametrize("n", [1, 2])
def test_owned_upload_variable_and_tfvars_are_immutable(tmp_path, intent, n):
    bucket = bucket_name("flaskr", n)
    _, binding, records, output = setup(tmp_path, intent, bucket=bucket)
    runtime = binding.runtime
    assert runtime.settings.storage_bucket == bucket
    framework = json.loads((runtime.work / "ddak.tf.json").read_bytes())
    assert framework["variable"]["upload_bucket"] == {"type": "string"}
    variables = runtime.work / "upload.auto.tfvars.json"
    assert json.loads(variables.read_bytes()) == {"upload_bucket": bucket}
    assert variables.read_bytes() == runtime._files[variables.name]
    assert variables.stat().st_mode & 0o777 == 0o600
    records.append(approval(output.plan_sha256))
    variables.write_text(json.dumps({"upload_bucket": bucket_name("flaskr", 3)}))
    sdk = runtime.foundation_clients(binding.read_session())["iam"]
    with pytest.raises(DdakToolError, match="파일이 바뀌었다"):
        runtime.apply(session=binding.apply_session())
    assert not sdk.writes
    assert not (runtime.work / "apply-started").exists()


@pytest.mark.parametrize(
    "bucket",
    [
        None,
        "gerbera-other-images-1",
        "gerbera-flaskr-images-01",
        "gerbera-flaskr-images-x-images-1",
    ],
)
@pytest.mark.parametrize("intent", ["create", "remove"])
def test_settings_reject_invalid_reserved_bucket(bucket, intent):
    with pytest.raises(DdakToolError, match="저장소 의도"):
        AwsSettings(
            "flaskr",
            ACCOUNT,
            "ddak-fixture-state",
            "app",
            {},
            storage_intent=intent,
            storage_bucket=bucket,
            task_role_arn=f"arn:aws:iam::{ACCOUNT}:role/ddak/app/flaskr-task",
        )


def test_numbered_bucket_changes_approval_hash_even_for_same_saved_plan(tmp_path):
    _, first, _, first_output = setup(tmp_path / "first")
    _, second, records, second_output = setup(tmp_path / "second", bucket=bucket_name("flaskr", 2))
    shared_plan_hash = "sha256:" + "a" * 64
    assert first.runtime._approval_hash(shared_plan_hash) != second.runtime._approval_hash(
        shared_plan_hash
    )
    assert first_output.plan_sha256 != second_output.plan_sha256
    records.append(approval(first_output.plan_sha256))
    with pytest.raises(DdakToolError, match="APPROVAL_REQUIRED"):
        second.runtime.apply(session=second.apply_session())
    sdk = second.runtime.foundation_clients(second.read_session())["iam"]
    assert not sdk.writes
    assert not (second.runtime.work / "apply-started").exists()


def test_repeated_storage_apply_creates_boundary_version_once_across_numbers(tmp_path):
    sdk = None
    for index, (intent, n) in enumerate((("create", 1), ("remove", 1), ("create", 2))):
        bucket = bucket_name("flaskr", n)
        _, binding, records, output = setup(tmp_path / str(index), intent, bucket=bucket, sdk=sdk)
        sdk = binding.runtime.foundation_clients(binding.read_session())["iam"]
        expected = "update" if index == 0 else "unchanged"
        assert output.summary["iam_diff"][-1]["boundary_changes"][0]["action"] == expected
        records.append(approval(output.plan_sha256))
        applied = binding.runtime.apply(session=binding.apply_session())
        assert len(sdk.writes) == 1
        assert applied["boundary_versions"][0]["status"] == (
            "updated" if index == 0 else "unchanged"
        )
        assert applied["outputs"] == ({"upload_bucket": bucket} if intent == "create" else {})


@pytest.mark.parametrize("phase", ["refresh", "apply"])
@pytest.mark.parametrize(
    "observed", [bucket_name("flaskr", 1), bucket_name("flaskr", 3), bucket_name("other", 2)]
)
def test_observed_bucket_must_equal_approved_reservation(tmp_path, phase, observed):
    _, binding, records, output = setup(tmp_path, bucket=bucket_name("flaskr", 2))
    runtime = binding.runtime
    runtime.runner.outputs["upload_bucket"] = observed
    records.append(approval(output.plan_sha256))
    with pytest.raises(DdakToolError, match="출력 갱신 실패") as exc:
        if phase == "apply":
            runtime.apply(session=binding.apply_session())
        else:
            runtime.refresh(session=binding.read_session())
    assert not (runtime.work / "apply-succeeded").exists()
    if phase == "apply":
        assert exc.value.needs_human


@pytest.mark.parametrize("intent", ["create", "remove"])
def test_storage_update_uses_one_approval_and_refreshes_output(tmp_path, intent):
    ctx, binding, records, output = setup(tmp_path, intent)
    runtime = binding.runtime
    summary = output.summary
    assert summary["counts"]["create" if intent == "create" else "delete"] == 4
    assert summary["storage"]["intent"] == intent
    assert summary["storage"]["source"] == "fixture"
    encoded = encode_meta(summary, infra=True)
    assert len(encoded.encode()) <= 8192
    assert ACCOUNT not in encoded
    assert "storage.tf" in summary["storage"]["files"]
    boundary = summary["iam_diff"][-1]
    assert boundary["address"] == "ddak.foundation.app_boundary"
    assert boundary["boundary_changes"][0]["action"] == "update"
    assert "s3:GetObject" in str(boundary)
    sdk = runtime.foundation_clients(binding.read_session())["iam"]
    with pytest.raises(DdakToolError, match="APPROVAL_REQUIRED"):
        runtime.apply(session=binding.apply_session())
    assert not sdk.writes
    records.append(approval(output.plan_sha256))
    applied = runtime.apply(session=binding.apply_session())
    assert len(sdk.writes) == 1
    assert sdk.writes[0]["SetAsDefault"] is True
    assert applied["boundary_versions"][0]["previous_version_id"] == "v1"
    assert applied["boundary_versions"][0]["new_version_id"] == "v2"
    assert runtime._foundation is None
    updated = refresh_infra_context(
        step("deploy.infra.cloud", "apply_infra"), {**applied, "passed": True, "layer": "app"}, ctx
    )
    if intent == "create":
        assert updated.platform["cloud"]["upload_bucket"] == BUCKET
    else:
        assert "upload_bucket" not in updated.platform["cloud"]
    assert updated.platform["cloud"]["task_role_arn"] == ctx.platform["cloud"]["task_role_arn"]


def test_storage_boundary_change_after_approval_stops_before_apply(tmp_path):
    _, binding, records, output = setup(tmp_path)
    records.append(approval(output.plan_sha256))
    sdk = binding.runtime.foundation_clients(binding.read_session())["iam"]
    sdk.version = "v3"
    with pytest.raises(DdakToolError, match="승인 이후"):
        binding.runtime.apply(session=binding.apply_session())
    assert not sdk.writes
    assert not (binding.runtime.work / "apply-succeeded").exists()


def test_storage_boundary_at_five_versions_does_not_delete_or_apply(tmp_path):
    _, binding, records, output = setup(tmp_path)
    records.append(approval(output.plan_sha256))
    sdk = binding.runtime.foundation_clients(binding.read_session())["iam"]
    sdk.list_policy_versions = lambda **kw: {
        "Versions": [{"VersionId": f"v{i}", "IsDefaultVersion": i == 1} for i in range(1, 6)],
        "IsTruncated": False,
    }
    with pytest.raises(DdakToolError, match="5개"):
        binding.runtime.apply(session=binding.apply_session())
    assert not sdk.writes


def test_storage_external_role_must_exist_with_boundary(tmp_path):
    ctx = context()
    binding = fixture_binding(ctx, root=tmp_path, approvals=lambda: [], guard=Mock())
    runtime = binding.runtime
    runtime.prepare_storage(session=binding.read_session())
    assert runtime.validate(binding.files).passed
    sdk = runtime.foundation_clients(binding.read_session())["iam"]
    sdk.get_role = lambda **kw: {"Role": {"RoleName": "flaskr-task"}}
    with pytest.raises(DdakToolError, match="태스크 역할"):
        runtime.plan(session=binding.read_session(), analyzer=binding.analyzer())


def test_storage_metadata_keeps_eight_kib_limit(tmp_path):
    _, _, _, output = setup(tmp_path)
    summary = json.loads(json.dumps(output.summary))
    summary["storage"]["files"]["storage.tf"] = "x" * 8192
    with pytest.raises(DdakToolError, match="메타"):
        encode_meta(summary, infra=True)


def test_remove_drops_legacy_flat_bucket_too():
    ctx = context("remove")
    ctx = replace(ctx, platform={**ctx.platform, "upload_bucket": BUCKET})
    updated = refresh_infra_context(
        step("deploy.infra.cloud", "apply_infra"),
        {"passed": True, "layer": "app", "outputs": {}},
        ctx,
    )
    assert "upload_bucket" not in updated.platform
    assert "upload_bucket" not in updated.platform["cloud"]
