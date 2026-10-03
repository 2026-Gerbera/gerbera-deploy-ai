"""업로드 저장소의 고정 주소·설정·외부 태스크 역할 계약."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping
from typing import Any

from ddak.core.storage import valid_bucket

from .policy import PolicyViolation, policy_json, require, statements, strings

STORAGE_ADDRESSES = frozenset(
    {
        "aws_s3_bucket.uploads",
        "aws_s3_bucket_public_access_block.uploads",
        "aws_s3_bucket_server_side_encryption_configuration.uploads",
        "aws_iam_role_policy.uploads",
    }
)
_PUBLIC_BLOCK = {
    "block_public_acls",
    "block_public_policy",
    "ignore_public_acls",
    "restrict_public_buckets",
}
# plan에서 값이 확정돼야 하는 최상위 속성. configuration에서 설정한 속성도 같은 기준을 따른다.
_KNOWN_REQUIRED = {
    "aws_s3_bucket": frozenset({"bucket", "force_destroy"}),
    "aws_s3_bucket_public_access_block": frozenset({"bucket", *_PUBLIC_BLOCK}),
    "aws_s3_bucket_server_side_encryption_configuration": frozenset({"bucket", "rule"}),
    "aws_iam_role_policy": frozenset({"name", "role", "policy"}),
}
# provider가 계산해 unknown일 수 있는 경로.
# 종속 리소스의 bucket은 configuration 참조로 따로 확인한다.
_UNKNOWN_ALLOWED = {
    "aws_s3_bucket_public_access_block": frozenset({"bucket"}),
    "aws_s3_bucket_server_side_encryption_configuration": frozenset(
        {
            "bucket",
            "rule[].bucket_key_enabled",
            "rule[].apply_server_side_encryption_by_default[].kms_master_key_id",
        }
    ),
}


def _reserved_bucket(project: str, name: str | None) -> bool:
    return isinstance(name, str) and valid_bucket(project, name)


def storage_resource(kind: str, address: str) -> bool:
    return kind.startswith("aws_s3_bucket") or address in STORAGE_ADDRESSES


def _flagged_paths(value: Any, path: tuple[str | int, ...] = ()) -> Iterator[tuple[str | int, ...]]:
    """after_unknown·*_sensitive에서 표시된 leaf 경로만 낸다.

    빈 dict·list와 false는 표시가 아니다.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _flagged_paths(item, (*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _flagged_paths(item, (*path, index))
    elif value is not False and value is not None:
        yield path


def _path_text(path: tuple[str | int, ...], *, pattern: bool = False) -> str:
    text = ""
    for part in path:
        if isinstance(part, int):
            text += "[]" if pattern else f"[{part}]"
        else:
            # map 키가 섞여도 값처럼 보이는 문자를 상세에 싣지 않는다.
            text += "." + re.sub(r"[^A-Za-z0-9_]", "?", part)[:64]
    return text.removeprefix(".")


def _require_known(
    kind: str, address: str, field: str, flags: Any, configured: Collection[str]
) -> None:
    require(isinstance(flags, dict), f"STORAGE_RESOURCE_UNKNOWN: {address} ({field})")
    where = "" if field == "after_unknown" else f" ({field})"
    allowed = _UNKNOWN_ALLOWED.get(kind, frozenset()) if field == "after_unknown" else frozenset()
    guarded = _KNOWN_REQUIRED[kind] | set(configured)
    for path in _flagged_paths(flags):
        if _path_text(path, pattern=True) in allowed:
            continue
        # 설정하지 않은 provider 계산 속성이 통째로 미확정인 경우만 넘긴다.
        if len(path) == 1 and path[0] not in guarded:
            continue
        raise PolicyViolation(f"STORAGE_RESOURCE_UNKNOWN: {address}.{_path_text(path)}{where}")


def external_task_role(
    external_roles: Mapping[str, Any] | None,
    *,
    project: str | None = None,
    account: str | None = None,
    boundary: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """호출자가 GetRole로 확인한 정규화 메타만 받는다. SDK 호출은 하지 않는다."""
    require(isinstance(external_roles, Mapping), "STORAGE_EXTERNAL_ROLE_REQUIRED")
    if project is None:
        require(len(external_roles) == 1, "STORAGE_EXTERNAL_ROLE_REQUIRED")
        name = next(iter(external_roles))
        require(isinstance(name, str) and name.endswith("-task"), "STORAGE_ROLE_SCOPE")
        project = name.removesuffix("-task")
    require(bool(re.fullmatch(r"[a-z][a-z0-9-]{0,39}", project)), "STORAGE_NAME_SCOPE")
    name = f"{project}-task"
    role = external_roles.get(name)
    require(isinstance(role, dict) and role.get("RoleName") == name, "STORAGE_ROLE_SCOPE")
    arn = role.get("Arn")
    require(isinstance(arn, str), "STORAGE_ROLE_SCOPE")
    match = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/ddak/app/" + re.escape(name), arn)
    require(match is not None, "STORAGE_ROLE_SCOPE")
    observed_account = match.group(1)
    require(account is None or observed_account == account, "STORAGE_ROLE_SCOPE")
    expected_boundary = f"arn:aws:iam::{observed_account}:policy/ddak/boundary/ddak-app-boundary"
    require(role.get("Path") == "/ddak/app/", "IAM_ROLE_PATH")
    permissions_boundary = role.get("PermissionsBoundary")
    require(isinstance(permissions_boundary, dict), "IAM_BOUNDARY_REQUIRED")
    require(
        permissions_boundary.get("PermissionsBoundaryArn") == expected_boundary
        and (boundary is None or expected_boundary == boundary),
        "IAM_BOUNDARY_REQUIRED",
    )
    return (
        project,
        observed_account,
        {
            "name": name,
            "arn": arn,
            "path": role["Path"],
            "permissions_boundary": permissions_boundary["PermissionsBoundaryArn"],
        },
    )


def inspect_storage_policy(value: Any, bucket: str) -> None:
    policy = policy_json(value)
    require(set(policy) <= {"Version", "Statement"}, "STORAGE_POLICY_SCOPE")
    require(policy.get("Version", "2012-10-17") == "2012-10-17", "STORAGE_POLICY_SCOPE")
    expected = {
        ("s3:GetObject", f"arn:aws:s3:::{bucket}/*"),
        ("s3:PutObject", f"arn:aws:s3:::{bucket}/*"),
        ("s3:ListBucket", f"arn:aws:s3:::{bucket}"),
    }
    observed = set()
    for row in statements(policy):
        require(
            row.get("Effect") == "Allow" and set(row) <= {"Sid", "Effect", "Action", "Resource"},
            "STORAGE_POLICY_SCOPE",
        )
        pairs = {
            (action, ref)
            for action in strings(row.get("Action"))
            for ref in strings(row.get("Resource"))
        }
        require(pairs <= expected, "STORAGE_POLICY_SCOPE")
        observed.update(pairs)
    require(observed == expected, "STORAGE_POLICY_SCOPE")


def inspect_storage_body(
    kind: str, body: dict[str, Any], *, bucket: str, role: str, hcl: bool = False
) -> None:
    expected_buckets = {"${var.upload_bucket}"} if hcl else {bucket}
    if hcl and kind != "aws_s3_bucket":
        expected_buckets |= {"${aws_s3_bucket.uploads.id}", "${aws_s3_bucket.uploads.bucket}"}
    require(
        body.get("bucket") in expected_buckets if kind != "aws_iam_role_policy" else True,
        "STORAGE_NAME_SCOPE",
    )
    if kind == "aws_s3_bucket":
        require(not body.get("bucket_prefix"), "STORAGE_NAME_SCOPE")
        require(body.get("force_destroy") is True, "STORAGE_FORCE_DESTROY")
        require(
            not body.get("policy") and body.get("acl") in (None, "private"), "STORAGE_PUBLIC_ACCESS"
        )
    elif kind == "aws_s3_bucket_public_access_block":
        require(all(body.get(key) is True for key in _PUBLIC_BLOCK), "STORAGE_PUBLIC_ACCESS")
    elif kind == "aws_s3_bucket_server_side_encryption_configuration":
        rules = body.get("rule")
        require(
            isinstance(rules, list) and len(rules) == 1 and isinstance(rules[0], dict),
            "STORAGE_ENCRYPTION",
        )
        default = rules[0].get("apply_server_side_encryption_by_default")
        require(
            isinstance(default, list)
            and len(default) == 1
            and isinstance(default[0], dict)
            and default[0].get("sse_algorithm") == "AES256"
            and not default[0].get("kms_master_key_id"),
            "STORAGE_ENCRYPTION",
        )
    else:
        require(body.get("role") == role, "STORAGE_ROLE_SCOPE")
        require(body.get("name") == "uploads" and not body.get("name_prefix"), "STORAGE_NAME_SCOPE")
        inspect_storage_policy(body.get("policy"), "${var.upload_bucket}" if hcl else bucket)


def inspect_storage_hcl(
    resources: list[tuple[str, str, dict[str, Any]]],
    *,
    layer: str,
    intent: str | None,
    external_roles: Mapping[str, Any] | None,
    project: str,
    account: str,
    storage_bucket: str | None = None,
) -> None:
    require(intent in (None, "create", "remove"), "STORAGE_INTENT")
    if intent is not None:
        require(_reserved_bucket(project, storage_bucket), "STORAGE_NAME_SCOPE")
    rows = [
        (kind, address, body)
        for kind, address, body in resources
        if storage_resource(kind, address)
    ]
    for kind, address, body in resources:
        if layer == "app" and kind == "aws_iam_role_policy" and address not in STORAGE_ADDRESSES:
            policy = policy_json(body.get("policy"))
            require(
                not any(
                    action.lower().startswith("s3:")
                    for row in statements(policy)
                    for action in strings(row.get("Action"))
                ),
                "STORAGE_POLICY_SCOPE",
            )
            require(
                not external_roles or body.get("role") not in external_roles, "STORAGE_ROLE_SCOPE"
            )
    if not rows:
        require(intent != "create", "STORAGE_RESOURCE_SET")
        require(intent != "remove" or layer == "app", "STORAGE_INTENT")
        return
    # 기존 platform 소스 버킷 경로는 그대로 유지한다.
    if (
        layer == "platform"
        and intent is None
        and not any(address in STORAGE_ADDRESSES for _, address, _ in rows)
    ):
        return
    require(layer == "app" and intent == "create", "STORAGE_INTENT")
    require({address for _, address, _ in rows} == STORAGE_ADDRESSES, "STORAGE_RESOURCE_SET")
    require(bool(re.fullmatch(r"[a-z][a-z0-9-]{0,39}", project)), "STORAGE_NAME_SCOPE")
    require(bool(re.fullmatch(r"[0-9]{12}", account)), "STORAGE_NAME_SCOPE")
    bucket = "${var.upload_bucket}"
    role_name = f"{project}-task"
    for kind, _, body in rows:
        # 변수로 표현한 이름도 이번 플랫폼의 단일 태스크 역할에만 대응한다.
        expected_role = (
            "${var.project}-task" if body.get("role") == "${var.project}-task" else role_name
        )
        inspect_storage_body(kind, body, bucket=bucket, role=expected_role, hcl=True)


def inspect_storage_plan(
    changes: list[dict[str, Any]],
    *,
    layer: str,
    update: bool,
    intent: str | None,
    project: str,
    account: str,
    boundary: str,
    external_roles: Mapping[str, Any] | None,
    configured_buckets: dict[str, str],
    storage_bucket: str | None = None,
    configured_keys: Mapping[str, Collection[str]] | None = None,
) -> set[str]:
    """삭제 예외를 부여하기 전에 네 리소스의 before까지 검증한다.

    configured_keys는 plan configuration의 리소스별 expressions 키다. 설정한 속성은
    provider 계산 속성처럼 unknown을 넘기지 않는다.
    """
    require(intent in (None, "create", "remove"), "STORAGE_INTENT")
    if intent is not None:
        require(_reserved_bucket(project, storage_bucket), "STORAGE_NAME_SCOPE")
    rows = [row for row in changes if storage_resource(row["type"], row["address"])]
    if (
        layer == "platform"
        and intent is None
        and not any(row["address"] in STORAGE_ADDRESSES for row in rows)
    ):
        return set()
    if not rows:
        require(intent is None, "STORAGE_RESOURCE_SET")
        return set()
    require(layer == "app" and update and intent in ("create", "remove"), "STORAGE_INTENT")
    require({row["address"] for row in rows} == STORAGE_ADDRESSES, "STORAGE_RESOURCE_SET")
    _, _, role = external_task_role(
        external_roles, project=project, account=account, boundary=boundary
    )
    bucket = storage_bucket or ""
    for row in rows:
        kind, address, change = row["type"], row["address"], row["change"]
        if intent == "remove":
            require(
                change.get("actions") == ["delete"]
                and change.get("after") is None
                and not change.get("replace_paths"),
                "UPDATE_DESTRUCTIVE",
            )
        else:
            require(
                change.get("actions") in (["create"], ["update"], ["no-op"]), "UPDATE_DESTRUCTIVE"
            )
            require(isinstance(change.get("after"), dict), "STORAGE_RESOURCE_UNKNOWN")
        configured = (configured_keys or {}).get(address, ())
        for field in ("after_unknown", "after_sensitive", "before_sensitive"):
            _require_known(kind, address, field, change.get(field) or {}, configured)
        for field in ("before", "after"):
            body = change.get(field)
            if body is None:
                require(field != "before" or intent != "remove", "STORAGE_RESOURCE_UNKNOWN")
                continue
            require(isinstance(body, dict), "STORAGE_RESOURCE_UNKNOWN")
            body = dict(body)
            # 신규 버킷의 id는 unknown일 수 있다. configuration의 직접 참조만 허용한다.
            if field == "after" and kind not in ("aws_s3_bucket", "aws_iam_role_policy"):
                unknown = change.get("after_unknown") or {}
                if unknown.get("bucket") is True:
                    require(
                        configured_buckets.get(address) == "aws_s3_bucket.uploads",
                        "STORAGE_NAME_SCOPE",
                    )
                    body["bucket"] = bucket
            require(
                body.get("region", "ap-northeast-2") == "ap-northeast-2"
                and not body.get("replica"),
                "PLAN_REGION",
            )
            inspect_storage_body(kind, body, bucket=bucket, role=role["name"])
    return set(STORAGE_ADDRESSES) if intent == "remove" else set()
