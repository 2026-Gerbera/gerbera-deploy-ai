"""source=fixture: 저장소 UPDATE의 단일 승인과 경계 버전·출력 연결."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from ddak.cloud.infra import bind_infra, fixture_binding, run_plan, run_validate, unbind_infra
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.tools.plan_infra import PlanInfraInput
from ddak.core.contracts.tools.validate_infra import ValidateInfraInput
from ddak.executor.approval_meta import encode_meta
from ddak.executor.infra import refresh_infra_context
from tests.unit.cloud.infra.test_runtime import approval
from tests.unit.test_deployment_service import step

ACCOUNT = "123456789012"
BUCKET = f"ddak-flaskr-uploads-{ACCOUNT}"


def context(intent="create", **kwargs):
    return RunContext(
        "run-1",
        project="flaskr",
        mode=RunMode.UPDATE,
        adapter_mode=AdapterMode.FAKE,
        targets="cloud",
        platform={
            "cloud": {
                "task_role_arn": f"arn:aws:iam::{ACCOUNT}:role/ddak/app/flaskr-task",
                **({"upload_bucket": BUCKET} if intent == "remove" else {}),
            }
        },
        project_settings={
            "_infra_storage": {
                "intent": intent,
                "bucket": BUCKET,
                "evidence": [{"file": "flaskr/uploads.py", "line": 7, "kind": "hardcoded_dir"}]
                if intent == "create"
                else [],
            }
        },
        **kwargs,
    )


def setup(tmp_path, intent="create"):
    ctx = context(intent)
    records = []
    binding = fixture_binding(ctx, root=tmp_path, approvals=lambda: records, guard=Mock())
    bind_infra(binding)
    try:
        assert run_validate(ValidateInfraInput(run_id=ctx.run_id), ctx).passed
        output = run_plan(PlanInfraInput(run_id=ctx.run_id), ctx)
    finally:
        unbind_infra(ctx.run_id)
    return ctx, binding, records, output


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
