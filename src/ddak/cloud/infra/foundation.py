"""코드 소유 기반 확보. 승인한 관측 상태에서만 경계 정책 버전을 추가한다."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from ddak.core.contracts.approval import ApprovalRecord
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.infra_evidence import BoundaryPolicyVersion

from .boundary_versions import apply_boundaries, check_boundaries
from .providers.aws import (
    REGION,
    bootstrap_dbinit_exception,
    boundary_document,
    build_boundary_document,
    ecs_infrastructure_role,
)
from .runtime import AwsSettings, canonical, check_approval, digest, private_write


def foundation_approval_hash(settings: AwsSettings, snapshots: Sequence[dict[str, Any]]) -> str:
    return digest(
        canonical({"template": foundation_template(settings), "boundaries": list(snapshots)})
    )


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
        "boundary": boundary_document(settings.account_id, settings.project),
        "boundary_arn": settings.boundary_arn,
        "build_boundary": build_boundary_document(settings.account_id),
        "build_boundary_arn": settings.build_boundary_arn,
        "ecs_infrastructure_role": ecs_infrastructure_role(settings.account_id),
        "bootstrap_dbinit_exception": bootstrap_dbinit_exception(settings.account_id),
    }


def _check_ecs_role(iam: Any, expected: dict[str, Any]) -> bool:
    try:
        role = iam.get_role(RoleName=expected["name"])["Role"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "NoSuchEntity":
            return False
        raise
    if (
        role.get("Arn") != expected["arn"]
        or role.get("RoleName") != expected["name"]
        or role.get("Path") != expected["path"]
        or canonical(role.get("AssumeRolePolicyDocument")) != canonical(expected["trust_policy"])
        or role.get("PermissionsBoundary")
    ):
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED, "기존 ECS 인프라 역할이 승인 템플릿과 다르다"
        )
    for operation, field in (
        (iam.list_attached_role_policies, "AttachedPolicies"),
        (iam.list_role_policies, "PolicyNames"),
    ):
        markers: set[str] = set()
        request = {"RoleName": expected["name"]}
        while True:
            page = operation(**request)
            policies = page.get(field)
            if not isinstance(policies, list) or (
                policies
                and (
                    field == "PolicyNames"
                    or any(
                        not isinstance(policy, dict)
                        or policy.get("PolicyArn") != expected["managed_policy_arn"]
                        for policy in policies
                    )
                )
            ):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "기존 ECS 인프라 역할에 승인 외 정책이 있다"
                )
            if page.get("IsTruncated") is False:
                break
            marker = page.get("Marker")
            if (
                page.get("IsTruncated") is not True
                or not isinstance(marker, str)
                or not marker
                or marker in markers
            ):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "기존 ECS 인프라 역할 정책 조회가 불완전하다"
                )
            markers.add(marker)
            request["Marker"] = marker
    return True


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
    expected_boundaries: Sequence[dict[str, Any]] | None = None,
    record_boundary: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """marker는 조립부가 project/run별 고정 영속 경로로 전달해야 한다.

    부분 실패 후에도 표식을 보존하고 동일 실행을 재시도하지 않는다.
    """
    template = foundation_template(settings)
    bound_to = digest(canonical(template))
    subject = foundation_approval_hash(settings, expected_boundaries or [])
    versions: list[BoundaryPolicyVersion] = []
    receipt_count = 0

    def record(row: dict[str, Any]) -> None:
        nonlocal receipt_count
        value = BoundaryPolicyVersion.model_validate(row)
        versions[:] = [old for old in versions if old.policy_arn != value.policy_arn]
        versions.append(value)
        # 시도 전 unknown과 응답 직후 version을 각각 영속화한다. 덮어쓰지 않는다.
        receipt_count += 1
        private_write(
            marker.with_name(marker.name + f"-boundary-{receipt_count}.json"),
            canonical(value.model_dump(mode="json")),
        )
        if record_boundary is not None:
            record_boundary(value.model_dump(mode="json"))

    guard()
    check_approval(
        approvals(),
        run_id=run_id,
        project=settings.run_project,
        kind="infra" if infra_subject else "foundation",
        bound_to=infra_subject or subject,
    )
    if expected_boundaries is None:
        raise DdakToolError(ErrorCode.APPROVAL_REQUIRED, "승인한 권한 경계 관측 상태가 없다")
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
        check_boundaries(settings, iam, expected_boundaries)
        ecs_role = template["ecs_infrastructure_role"]
        role_exists = _check_ecs_role(iam, ecs_role)
        private_write(marker, bound_to.encode())
        guard()
        if not bucket_exists:
            s3.create_bucket(
                Bucket=settings.state_bucket,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )
            private_write(
                marker.with_name(marker.name + "-bucket-created.json"),
                canonical(
                    {
                        "bucket": settings.state_bucket,
                        "account_id": settings.account_id,
                        "project": settings.project,
                        "run_id": run_id,
                        "status": "created_before_tagging",
                    }
                ),
            )
        kwargs = {"Bucket": settings.state_bucket, "ExpectedBucketOwner": settings.account_id}
        # 새 버킷은 가능한 한 먼저 관리 태그를 쓴다. 이 호출도 실패하면 생성 증거를 보존한다.
        s3.put_bucket_tagging(
            **kwargs,
            Tagging={
                "TagSet": [
                    {"Key": k, "Value": v} for k, v in {**existing, **template["tags"]}.items()
                ]
            },
        )
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
        apply_boundaries(settings, iam, expected_boundaries, guard=guard, record=record)
        guard()
        if not role_exists:
            try:
                iam.create_role(
                    Path=ecs_role["path"],
                    RoleName=ecs_role["name"],
                    AssumeRolePolicyDocument=json.dumps(ecs_role["trust_policy"]),
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "EntityAlreadyExists":
                    raise
                if not _check_ecs_role(iam, ecs_role):
                    raise DdakToolError(
                        ErrorCode.PRECONDITION_FAILED, "동시 생성된 ECS 인프라 역할 확인 실패"
                    ) from None
        guard()
        # 같은 관리형 정책의 재부착은 멱등이다. 기존 경계·신뢰 정책은 바꾸지 않는다.
        iam.attach_role_policy(RoleName=ecs_role["name"], PolicyArn=ecs_role["managed_policy_arn"])
        return {
            "state_bucket": settings.state_bucket,
            "app_boundary_arn": settings.boundary_arn,
            "build_boundary_arn": settings.build_boundary_arn,
            "template_sha256": bound_to,
            "boundary_versions": [version.model_dump(mode="json") for version in versions],
        }
    except DdakToolError as exc:
        exc.boundary_versions = tuple(versions)
        exc.needs_human = exc.needs_human or marker.exists()
        raise
    except Exception:
        raise DdakToolError(
            ErrorCode.ADAPTER_FAILED,
            "기반 준비 실패; 대상 상태 확인 후 다시 승인해야 한다",
            needs_human=marker.exists(),
            boundary_versions=versions,
        ) from None
