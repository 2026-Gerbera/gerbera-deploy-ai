"""source=fixture: 저장소 UPDATE의 SDK와 명령 경계를 메모리로 제공한다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.project_settings import cloud_platform_name
from ddak.core.storage import OUTPUT_KEY

from .bindings import InfraBinding
from .fixture import _FixtureRunner, _FixtureSDK, _FixtureSession
from .providers.aws import boundary_document
from .runtime import AwsSettings, InfraRuntime, SessionKeys
from .storage_bundle import storage_bundle

_ACCOUNT = "123456789012"


def storage_hcl(platform: str, account: str) -> str:
    bucket = "${var.upload_bucket}"
    policy = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject"],
                    "Resource": f"arn:aws:s3:::{bucket}/*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket"],
                    "Resource": f"arn:aws:s3:::{bucket}",
                },
            ],
        }
    )
    return f'''resource "aws_s3_bucket" "uploads" {{
  bucket = var.upload_bucket
  force_destroy = true
}}
resource "aws_s3_bucket_public_access_block" "uploads" {{
  bucket = var.upload_bucket
  block_public_acls = true
  block_public_policy = true
  ignore_public_acls = true
  restrict_public_buckets = true
}}
resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {{
  bucket = var.upload_bucket
  rule {{ apply_server_side_encryption_by_default {{ sse_algorithm = "AES256" }} }}
}}
resource "aws_iam_role_policy" "uploads" {{
  name = "uploads"
  role = "{platform}-task"
  policy = {json.dumps(policy)}
}}
'''


class StorageSDK(_FixtureSDK):
    def __init__(self, settings: AwsSettings):
        self.settings = settings
        self.version = "v1"
        self.document = boundary_document(settings.account_id, settings.project)
        self.document["Statement"] = [
            row
            for row in self.document["Statement"]
            if not any(
                str(action).startswith("s3:")
                for action in (
                    row["Action"] if isinstance(row["Action"], list) else [row["Action"]]
                )
            )
        ]
        self.writes: list[dict[str, Any]] = []

    def get_role(self, **kwargs):
        return {
            "Role": {
                "RoleName": f"{self.settings.project}-task",
                "Arn": self.settings.task_role_arn,
                "Path": "/ddak/app/",
                "PermissionsBoundary": {"PermissionsBoundaryArn": self.settings.boundary_arn},
            }
        }

    def get_policy(self, **kwargs):
        return {"Policy": {"Arn": kwargs["PolicyArn"], "DefaultVersionId": self.version}}

    def get_policy_version(self, **kwargs):
        return {
            "PolicyVersion": {
                "Document": self.document,
                "VersionId": self.version,
                "IsDefaultVersion": True,
            }
        }

    def list_policy_versions(self, **kwargs):
        return {
            "Versions": [{"VersionId": self.version, "IsDefaultVersion": True}],
            "IsTruncated": False,
        }

    def create_policy_version(self, **kwargs):
        self.writes.append(kwargs)
        self.version = "v2"
        self.document = json.loads(kwargs["PolicyDocument"])
        return {"PolicyVersion": {"VersionId": self.version, "IsDefaultVersion": True}}


def storage_fixture_binding(
    ctx: RunContext, *, root: Path, approvals: Any, guard: Any
) -> InfraBinding:
    platform = cloud_platform_name(ctx.project, ctx.project_settings)
    intent = ctx.project_settings["_infra_storage"]["intent"]
    bucket = ctx.project_settings["_infra_storage"]["bucket"]
    create = intent == "create"
    settings = AwsSettings(
        platform,
        _ACCOUNT,
        "ddak-fixture-state",
        "app",
        {OUTPUT_KEY: ("aws_s3_bucket.uploads.id", "string")} if create else {},
        approval_project=ctx.project,
        storage_intent=intent,
        storage_bucket=bucket,
        task_role_arn=f"arn:aws:iam::{_ACCOUNT}:role/ddak/app/{platform}-task",
    )
    hcl = storage_hcl(platform, _ACCOUNT)
    files = storage_bundle(
        hcl if create else "# 업로드 저장소 제거\n",
        ctx,
        (
            "업로드 소스의 저장소 근거를 확인한다.",
            "ECS 로컬 디스크는 태스크 사이에 공유되지 않는다."
            if create
            else "복구 소스는 업로드 디렉토리를 사용하지 않는다.",
            "고정된 네 저장소 리소스를 생성한다."
            if create
            else "고정된 네 저장소 리소스를 제거한다.",
            "근거와 Terraform을 한 화면에서 승인한다.",
            "승인 뒤 적용하고 출력을 기록한다."
            if create
            else "승인 뒤 적용하고 upload_bucket 출력을 제거한다.",
        ),
        Source.FIXTURE,
    )
    resolved = hcl.replace("${var.upload_bucket}", bucket).replace(
        "bucket = var.upload_bucket", f"bucket = {json.dumps(bucket)}"
    )
    runner = _FixtureRunner(resolved, {OUTPUT_KEY: bucket} if create else {})
    if not create:
        for resource in runner.raw["resource_changes"]:
            change = resource["change"]
            change.update(actions=["delete"], before=change["after"], after=None)
    sdk = StorageSDK(settings)
    session = SessionKeys("fixture-access", "fixture-value", "fixture-session")
    runtime = InfraRuntime(
        root=root,
        run_id=ctx.run_id,
        settings=settings,
        lock_file=b"source=fixture\n",
        approvals=approvals,
        guard=guard,
        runner=runner,
        foundation_clients=lambda _: {service: sdk for service in ("s3", "iam", "sts")},
        aws_project_settings={"aws_profile": "fixture", "aws_expected_account_id": _ACCOUNT},
        session_factory=_FixtureSession,
    )

    class Analyzer:
        def validate_policy(self, **kwargs):
            return {"findings": []}

    return InfraBinding(
        runtime,
        files,
        ctx.adapter_mode,
        lambda: session,
        lambda: session,
        Analyzer,
        generation_source=Source.FIXTURE,
    )
