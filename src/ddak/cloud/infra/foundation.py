"""코드 소유 기반 템플릿과 승인 후 생성. 자동 삭제·정책 버전 교체는 하지 않는다."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from ddak.core.contracts.approval import ApprovalRecord
from ddak.core.contracts.errors import DdakToolError, ErrorCode

from .providers.aws import (
    BOUNDARY_NAME,
    BOUNDARY_PATH,
    REGION,
    boundary_document,
    build_boundary_document,
)
from .runtime import AwsSettings, canonical, check_approval, digest, private_write


def foundation_template(settings: AwsSettings) -> dict[str, Any]:
    return {
        "bucket": settings.state_bucket,
        "region": REGION,
        "account_id": settings.account_id,
        "tags": {"ManagedBy": "ddak", "Project": settings.project},
        "versioning": "Enabled",
        "encryption": "AES256",
        "block_public_access": True,
        "bucket_policy": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:*",
                    "Resource": [
                        f"arn:aws:s3:::{settings.state_bucket}",
                        f"arn:aws:s3:::{settings.state_bucket}/*",
                    ],
                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                }
            ],
        },
        "boundary": boundary_document(settings.account_id),
        "boundary_arn": settings.boundary_arn,
        "build_boundary": build_boundary_document(settings.account_id),
        "build_boundary_arn": settings.build_boundary_arn,
    }


def apply_foundation(
    *,
    settings: AwsSettings,
    run_id: str,
    s3: Any,
    iam: Any,
    sts: Any,
    approvals: Callable[[], Sequence[ApprovalRecord]],
    guard: Callable[[], None],
    marker: Path,
    infra_subject: str | None = None,
    expected_bucket_exists: bool | None = None,
) -> dict[str, str]:
    """marker는 조립부가 project/run별 고정 영속 경로로 전달해야 한다.

    부분 실패 후에도 표식을 보존하고 동일 실행을 재시도하지 않는다.
    """
    template = foundation_template(settings)
    bound_to = digest(canonical(template))
    guard()
    check_approval(
        approvals(),
        run_id=run_id,
        project=settings.project,
        kind="infra" if infra_subject else "foundation",
        bound_to=infra_subject or bound_to,
    )
    if marker.exists():
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "이전 기반 적용 상태를 확인해야 한다")
    try:
        if sts.get_caller_identity().get("Account") != settings.account_id:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "기반 준비 세션의 대상 계정이 다르다"
            )
        bucket_exists = True
        try:
            s3.head_bucket(Bucket=settings.state_bucket, ExpectedBucketOwner=settings.account_id)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
                raise
            bucket_exists = False
        if expected_bucket_exists is not None and bucket_exists != expected_bucket_exists:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "계획 이후 기반 버킷 상태가 바뀌었다"
            )
        existing = {}
        if bucket_exists:
            tags = s3.get_bucket_tagging(
                Bucket=settings.state_bucket, ExpectedBucketOwner=settings.account_id
            )["TagSet"]
            existing = {x["Key"]: x["Value"] for x in tags}
            if any(existing.get(k) != v for k, v in template["tags"].items()):
                raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "기반 버킷 관리 태그가 다르다")
            try:
                policy = json.loads(
                    s3.get_bucket_policy(
                        **{
                            "Bucket": settings.state_bucket,
                            "ExpectedBucketOwner": settings.account_id,
                        }
                    )["Policy"]
                )
                if canonical(policy) != canonical(template["bucket_policy"]):
                    raise DdakToolError(
                        ErrorCode.PRECONDITION_FAILED, "기존 버킷 TLS 정책이 다르다"
                    )
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "NoSuchBucketPolicy":
                    raise
        policies = [
            (BOUNDARY_NAME, settings.boundary_arn, template["boundary"]),
            ("ddak-build-boundary", settings.build_boundary_arn, template["build_boundary"]),
        ]
        create_policies = []
        for name, arn, document in policies:
            try:
                policy = iam.get_policy(PolicyArn=arn)["Policy"]
                current = iam.get_policy_version(
                    PolicyArn=arn, VersionId=policy["DefaultVersionId"]
                )["PolicyVersion"]["Document"]
                if canonical(current) != canonical(document):
                    raise DdakToolError(
                        ErrorCode.PRECONDITION_FAILED, "기존 권한 경계가 템플릿과 다르다"
                    )
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "NoSuchEntity":
                    raise
                create_policies.append((name, document))
        private_write(marker, bound_to.encode())
        guard()
        if not bucket_exists:
            s3.create_bucket(
                Bucket=settings.state_bucket,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )
        kwargs = {"Bucket": settings.state_bucket, "ExpectedBucketOwner": settings.account_id}
        s3.put_public_access_block(
            **kwargs,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        s3.put_bucket_policy(**kwargs, Policy=json.dumps(template["bucket_policy"]))
        s3.put_bucket_versioning(**kwargs, VersioningConfiguration={"Status": "Enabled"})
        s3.put_bucket_encryption(
            **kwargs,
            ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            },
        )
        s3.put_bucket_tagging(
            **kwargs,
            Tagging={
                "TagSet": [
                    {"Key": k, "Value": v} for k, v in {**existing, **template["tags"]}.items()
                ]
            },
        )
        for name, document in create_policies:
            iam.create_policy(
                PolicyName=name,
                Path=BOUNDARY_PATH,
                PolicyDocument=json.dumps(document),
                Tags=[{"Key": k, "Value": v} for k, v in template["tags"].items()],
            )
        return {
            "state_bucket": settings.state_bucket,
            "app_boundary_arn": settings.boundary_arn,
            "build_boundary_arn": settings.build_boundary_arn,
            "template_sha256": bound_to,
        }
    except DdakToolError:
        raise
    except Exception:
        raise DdakToolError(
            ErrorCode.ADAPTER_FAILED, "기반 준비 실패; 대상 상태 확인 후 다시 승인해야 한다"
        ) from None
