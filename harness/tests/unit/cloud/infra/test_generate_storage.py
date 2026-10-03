"""저장소 생성기·캐시·요약의 fixture 계약. 외부 서비스나 실행기는 호출하지 않는다."""

import json
import sys
from dataclasses import replace
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from ddak.cloud.infra import storage_bundle as bundle
from ddak.cloud.infra.tools.generate_infra import logic
from ddak.cloud.infra.tools.generate_infra.storage import StorageDraft
from ddak.core.config import AdapterMode, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode, Source
from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.tools.generate_infra import GenerateInfraInput
from ddak.core.snapshots import digest_bytes

RATIONALE = (
    "flaskr/uploads.py:7은 업로드 이미지를 로컬 디렉터리에 저장한다.",
    "ECS 태스크는 디스크를 공유하지 않으므로 S3 저장소가 필요하다.",
    "고정 이름과 태스크 역할로 저장소 리소스 4개를 제안한다.",
    "근거와 Terraform 및 코드 변경을 표시하고 승인을 받는다.",
    "승인 후 적용과 출력 기록 및 주입과 배포와 스모크를 진행한다.",
)


def context(intent="create", **settings):
    return RunContext(
        "run-storage",
        adapter_mode=AdapterMode.FAKE,
        project="flaskr",
        mode=RunMode.UPDATE,
        project_settings={
            "_infra_storage": {
                "intent": intent,
                "evidence": [{"file": "flaskr/uploads.py", "line": 7, "kind": "file_write"}]
                if intent == "create"
                else [],
                "bucket": "ddak-flaskr-uploads-123456789012",
            },
            **settings,
        },
    )


def hcl():
    return """resource "aws_s3_bucket" "uploads" {
  bucket = "ddak-flaskr-uploads-123456789012"
  force_destroy = true
}
resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  block_public_acls = true
  block_public_policy = true
  ignore_public_acls = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_iam_role_policy" "uploads" {
  name = "uploads"
  role = "flaskr-task"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"],
      Resource = "arn:aws:s3:::ddak-flaskr-uploads-123456789012/*" },
    { Effect = "Allow", Action = ["s3:ListBucket"],
      Resource = "arn:aws:s3:::ddak-flaskr-uploads-123456789012" }
  ] })
}
"""


@pytest.fixture(autouse=True)
def fake_core_storage(monkeypatch):
    # 공통 모듈을 작성하는 작업과 독립적으로 생성기 계약만 확인한다.
    core = ModuleType("ddak.core.storage")
    core.STORAGE_ENV_KEY = "IMG_DIR"
    core.OUTPUT_KEY = "upload_bucket"
    core.bucket_name = lambda platform, account_id: f"ddak-{platform}-uploads-{account_id}"
    monkeypatch.setitem(sys.modules, "ddak.core.storage", core)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings()))
    monkeypatch.setattr(logic, "call_ai", Mock(side_effect=AssertionError("예상하지 않은 호출")))


def generate(root, ctx=None):
    directory = root / "bundles" / "run-storage"
    directory.mkdir(parents=True)
    result = logic.generate_infra(
        GenerateInfraInput(run_id="run-storage", directory=str(directory), layer="app"),
        ctx or context(),
    )
    files = {name: (directory / name).read_text() for name in result.files}
    return result, files


def ai_result(source=Source.FIXTURE):
    return SimpleNamespace(
        value=StorageDraft(
            rationale=RATIONALE,
            files=(logic.TerraformFileDraft(name="storage.tf", lines=tuple(hcl().splitlines())),),
        ),
        source=source,
    )


def test_create_uses_only_evidence_identity_and_separate_prompt(monkeypatch, tmp_path):
    request = Mock(return_value=ai_result())
    monkeypatch.setattr(logic, "call_ai", request)
    ctx = context(cloud_domain="excluded.example", unrelated_setting="exclude-this")
    raw = ctx.project_settings["_infra_storage"]
    raw["evidence"][0]["value"] = "exclude-this-too"
    result, files = generate(tmp_path, ctx)
    sent = request.call_args.kwargs
    assert json.loads(sent["data"]) == {
        "platform": "flaskr",
        "account": "123456789012",
        "task_role_name": "flaskr-task",
        "evidence": [{"file": "flaskr/uploads.py", "line": 7, "kind": "file_write"}],
    }
    assert sent["output_model"] is StorageDraft
    assert sent["prompt_version"] == bundle.STORAGE_PROMPT_VERSION != logic.PROMPT_VERSION
    assert logic.PROMPT_VERSION == "infra-aws-v3-rolling"
    assert sent["settings"].adapter_mode is AdapterMode.FAKE
    assert result.layer == "app" and result.source is Source.FIXTURE
    assert result.outputs == {"upload_bucket": ("aws_s3_bucket.uploads.id", "string")}
    assert files["storage.tf"].partition("\n")[2] == hcl()
    from ddak.cloud.infra.policy import static_gate

    assert static_gate(
        files, layer="app", storage_intent="create", project="flaskr", account_id="123456789012"
    ).passed
    assert result.files == {name: digest_bytes(value.encode()) for name, value in files.items()}
    assert not (tmp_path / "infra-baselines").exists()
    summary = bundle.storage_summary(files, ctx, result.source)
    assert len(summary["rationale"]) == 5
    assert summary["source"] == "fixture"
    assert summary["bucket"] == "ddak-flaskr-uploads-************"
    assert summary["env"] == {"IMG_DIR": "s3://ddak-flaskr-uploads-************/img"}
    assert "123456789012" not in json.dumps(summary)
    assert len(json.dumps({"storage": summary, "counts": {"create": 4}}).encode()) <= 8192


def test_remove_has_no_resources_outputs_or_ai(tmp_path):
    ctx = context("remove")
    result, files = generate(tmp_path, ctx)
    logic.call_ai.assert_not_called()
    assert result.outputs == {}
    assert all(line.startswith("#") for line in files["storage.tf"].splitlines())
    summary = bundle.storage_summary(files, ctx, result.source)
    assert summary["intent"] == "remove"
    assert len(summary["rationale"]) == 5
    assert summary["env"] == {"IMG_DIR": "img"}


def test_cache_is_explicit_versioned_and_bound_to_account_platform_role(monkeypatch, tmp_path):
    request = Mock(return_value=ai_result(Source.LIVE))
    monkeypatch.setattr(logic, "call_ai", request)
    ctx = context()
    result, files = generate(tmp_path, ctx)
    assert result.source is Source.LIVE
    assert bundle.load_storage_baseline(tmp_path, ctx) is None
    bundle.save_storage_baseline(tmp_path, "flaskr", files)
    assert bundle.load_storage_baseline(tmp_path, ctx) == files
    assert next((tmp_path / "infra-baselines" / "flaskr").iterdir()).name == (
        bundle.STORAGE_PROMPT_VERSION
    )
    role_ctx = replace(
        ctx, platform={"cloud": {"task_role_arn": "arn:aws:iam::123456789012:role/flaskr-other"}}
    )
    assert bundle.load_storage_baseline(tmp_path, role_ctx) is None
    account_ctx = context(
        aws_expected_account_id="210987654321",
        _infra_storage={
            "intent": "create",
            "evidence": ctx.project_settings["_infra_storage"]["evidence"],
        },
    )
    assert bundle.load_storage_baseline(tmp_path, account_ctx) is None
    platform_ctx = replace(
        ctx,
        project_settings={
            "cloud_platform": "other",
            "_infra_storage": {
                "intent": "create",
                "evidence": ctx.project_settings["_infra_storage"]["evidence"],
            },
        },
    )
    assert bundle.load_storage_baseline(tmp_path, platform_ctx) is None
    current = tmp_path / "bundles" / "run-storage-next"
    current.mkdir()
    cached = logic.generate_infra(
        GenerateInfraInput(run_id=ctx.run_id, directory=str(current), layer="app"), ctx
    )
    assert cached.source is Source.CACHE
    assert request.call_count == 1
    assert (current / "storage.tf").read_text() == files["storage.tf"]
    assert bundle.storage_summary(files, ctx, cached.source)["source"] == "cache"


@pytest.mark.parametrize("intent,mode", [("other", RunMode.UPDATE), ("create", RunMode.BOOTSTRAP)])
def test_app_requires_update_and_explicit_intent(tmp_path, intent, mode):
    with pytest.raises(DdakToolError):
        generate(tmp_path, replace(context(intent), mode=mode))
    logic.call_ai.assert_not_called()


def test_five_rationale_steps_and_hcl_summary_limits():
    with pytest.raises(ValidationError):
        StorageDraft(files=ai_result().value.files, rationale=RATIONALE[:4])
    with pytest.raises(DdakToolError, match="3KiB"):
        bundle.storage_bundle("x" * 3073, context(), RATIONALE, Source.FIXTURE)
    files = bundle.storage_bundle(hcl(), context(), RATIONALE, Source.REPLAY)
    summary = bundle.storage_summary(files, context(), Source.REPLAY)
    assert summary["source"] == "fixture"
    assert len(json.dumps(summary).encode()) <= bundle.MAX_STORAGE_SUMMARY_BYTES


def test_live_bundle_is_cached_only_after_validation_and_plan(monkeypatch, tmp_path):
    from ddak.cloud.infra import bind_infra, fixture_binding, run_plan, run_validate, unbind_infra
    from ddak.core.contracts.tools.plan_infra import PlanInfraInput
    from ddak.core.contracts.tools.validate_infra import ValidateInfraInput

    ctx = context()
    request = Mock(return_value=ai_result(Source.LIVE))
    monkeypatch.setattr(logic, "call_ai", request)
    output, files = generate(tmp_path, ctx)
    assert bundle.load_storage_baseline(tmp_path, ctx) is None
    binding = fixture_binding(
        ctx, root=tmp_path / "runtime", approvals=lambda: [], guard=lambda: None
    )
    binding = replace(binding, files=files, generation_source=output.source, baseline_root=tmp_path)
    bind_infra(binding)
    try:
        assert run_validate(ValidateInfraInput(run_id=ctx.run_id), ctx).passed
        assert bundle.load_storage_baseline(tmp_path, ctx) is None
        planned = run_plan(PlanInfraInput(run_id=ctx.run_id), ctx)
        assert planned.passed
        assert planned.summary["storage"]["source"] == "live"
    finally:
        unbind_infra(ctx.run_id)
    assert bundle.load_storage_baseline(tmp_path, ctx) == files
    directory = tmp_path / "bundles" / "next"
    directory.mkdir()
    cached = logic.generate_infra(
        GenerateInfraInput(run_id=ctx.run_id, directory=str(directory), layer="app"), ctx
    )
    assert cached.source == Source.CACHE
    assert request.call_count == 1
