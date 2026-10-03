"""Terraform show JSON의 제한된 승인 요약. 원문 state/plan을 저장하거나 로그로 내보내지 않는다."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ddak.core.redact import redact

from .policy import (
    PolicyViolation,
    inspect_ecs,
    inspect_policy,
    policy_json,
    protect_platform_resource,
    require,
    statements,
    strings,
)
from .providers.aws import APP_RESOURCE_TYPES, RESOURCE_TYPES


def _masked(value: str) -> str:
    # IAM diff에는 검증된 action/ARN만 넣으며 계정은 항상 가린다.
    if value.startswith("arn:aws:") and ":secret:rds!" in value:
        return "RDS master ARN sha256 " + hashlib.sha256(value.encode()).hexdigest()[:16]
    return re.sub(r"\b\d{12}\b", "************", value)


def _policy_view(policy: dict[str, Any]) -> list[dict[str, Any]]:
    """효과·조건·Deny 제거까지 보이되, 정책 문자열로 민감값을 운반하지 못하게 한다."""

    def safe(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: safe(child) for key, child in value.items()}
        if isinstance(value, list):
            return [safe(child) for child in value]
        if isinstance(value, str):
            masked = _masked(value)
            if re.fullmatch(r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\*{12}:[\w/*?.:@+=-]+", masked):
                return masked
            return redact(masked, max_len=512)
        return value

    return [safe({k: v for k, v in row.items() if k != "Sid"}) for row in statements(policy)]


def _configured_role_addresses(raw: dict[str, Any]) -> dict[str, str]:
    """plan에서 아직 unknown인 role ID를 코드의 직접 참조 주소로 해석한다."""
    root = (raw.get("configuration") or {}).get("root_module") or {}
    resources = root.get("resources") or []
    if not isinstance(resources, list):
        return {}
    result: dict[str, str] = {}
    for item in resources:
        if not isinstance(item, dict) or item.get("type") != "aws_iam_role_policy":
            continue
        address = item.get("address")
        role = (item.get("expressions") or {}).get("role") or {}
        references = role.get("references") or []
        if not isinstance(address, str) or not isinstance(references, list):
            continue
        matches = {
            match.group(1)
            for reference in references
            if isinstance(reference, str)
            and (
                match := re.fullmatch(
                    r"(aws_iam_role\.[A-Za-z][A-Za-z0-9_]*)\.(?:id|name)", reference
                )
            )
        }
        if len(matches) == 1:
            result[address] = matches.pop()
    return result


def _configured_bucket_addresses(raw: dict[str, Any]) -> dict[str, str]:
    """미확정 S3 종속 리소스의 bucket을 직접 참조한 버킷 주소로 해석한다."""
    root = (raw.get("configuration") or {}).get("root_module") or {}
    resources = root.get("resources") or []
    if not isinstance(resources, list):
        return {}
    result: dict[str, str] = {}
    for item in resources:
        if not isinstance(item, dict) or not str(item.get("type", "")).startswith("aws_s3_bucket_"):
            continue
        address = item.get("address")
        bucket = (item.get("expressions") or {}).get("bucket") or {}
        references = bucket.get("references") or []
        if not isinstance(address, str) or not isinstance(references, list):
            continue
        matches = {
            match.group(1)
            for reference in references
            if isinstance(reference, str)
            and (
                match := re.fullmatch(
                    r"(aws_s3_bucket\.[A-Za-z][A-Za-z0-9_]*)\.(?:id|bucket)", reference
                )
            )
        }
        if len(matches) == 1:
            result[address] = matches.pop()
    return result


def _valid_plan_address(resource: dict[str, Any]) -> bool:
    """일반 주소와 ACM DNS 검증 레코드의 제한된 for_each 주소만 허용한다."""
    kind = resource.get("type")
    name = resource.get("name")
    address = resource.get("address")
    if not isinstance(kind, str) or not isinstance(address, str):
        return False
    if name is None and "index" not in resource:
        prefix = f"{kind}."
        name = address.removeprefix(prefix) if address.startswith(prefix) else None
    if not isinstance(name, str):
        return False
    base = f"{kind}.{name}"
    if not re.fullmatch(r"aws_[a-z0-9_]+\.[A-Za-z][A-Za-z0-9_]*", base):
        return False
    if "index" not in resource:
        return address == base
    index = resource.get("index")
    return (
        kind == "aws_route53_record"
        and name == "certificate_validation"
        and isinstance(index, str)
        and bool(re.fullmatch(r"[A-Za-z0-9*_.-]{1,253}", index))
        and address == f"{base}[{json.dumps(index)}]"
    )


def summarize_plan(
    raw: dict[str, Any],
    *,
    layer: str,
    plan_sha256: str,
    exit_code: int,
    account_id: str,
    boundary_arn: str,
    update: bool,
    analyzer: Any,
    checkov: dict[str, Any],
    project: str = "",
    build_boundary_arn: str | None = None,
    rds_master_secret_arn: str | None = None,
    state_bucket: str | None = None,
) -> dict[str, Any]:
    """정책 불합격은 PolicyViolation, API 수행 실패는 호출자가 DdakToolError로 바꾼다."""
    require(raw.get("format_version", "").startswith("1."), "PLAN_FORMAT")
    require(exit_code in (0, 2), "PLAN_EXIT")
    require(
        raw.get("errored", False) is False and raw.get("complete", True) is True, "PLAN_INCOMPLETE"
    )
    changes = raw.get("resource_changes", [])
    require(isinstance(changes, list), "PLAN_FORMAT")
    counts = dict.fromkeys(("create", "update", "delete", "replace"), 0)
    destructive: list[str] = []
    iam_diff: list[dict[str, Any]] = []
    errors = warnings = 0
    roles = {}
    roles_by_address = {}
    buckets_by_address = {}
    configured_roles = _configured_role_addresses(raw)
    configured_buckets = _configured_bucket_addresses(raw)
    for resource in changes:
        if resource.get("type") == "aws_s3_bucket":
            after = resource["change"].get("after") or {}
            address = resource.get("address")
            if isinstance(address, str):
                buckets_by_address[address] = after
        if resource.get("type") == "aws_iam_role":
            after = resource["change"].get("after") or {}
            address = resource.get("address")
            if isinstance(address, str):
                roles_by_address[address] = after
            if after.get("name"):
                roles[after["name"]] = after
    for resource in changes:
        kind = resource.get("type")
        require(kind in (APP_RESOURCE_TYPES if layer == "app" else RESOURCE_TYPES), "PLAN_RESOURCE")
        require(resource.get("mode") == "managed", "PLAN_RESOURCE_MODE")
        address = resource.get("address", "")
        require(_valid_plan_address(resource), "PLAN_ADDRESS")
        change = resource["change"]
        # no-op/삭제·이전 이름 변경도 코드 소유 기반 버킷에는 허용하지 않는다.
        if kind.startswith("aws_s3_bucket") and state_bucket is not None:
            if change.get("after") is not None:
                after = change["after"]
                if (change.get("after_unknown") or {}).get("bucket"):
                    after = buckets_by_address.get(configured_buckets.get(address, ""))
                require(isinstance(after, dict), "FOUNDATION_BUCKET_UNRESOLVED")
                require(
                    isinstance(after.get("bucket"), str) and bool(after["bucket"]),
                    "FOUNDATION_BUCKET_UNRESOLVED",
                )
                protect_platform_resource(kind, after, state_bucket)
            for value in (change.get("before"), change.get("after")):
                if value and not (change.get("after_unknown") or {}).get("bucket"):
                    protect_platform_resource(kind, value, state_bucket)
        if kind == "aws_codebuild_project" and change.get("after"):
            protect_platform_resource(kind, change["after"], state_bucket)
        if (
            kind in {"aws_ecs_service", "aws_ecs_task_definition"}
            and change.get("after") is not None
        ):
            inspect_ecs(kind, change["after"])
        actions = change.get("actions")
        require(
            actions
            in (
                ["no-op"],
                ["create"],
                ["update"],
                ["delete"],
                ["delete", "create"],
                ["create", "delete"],
            ),
            "PLAN_ACTION",
        )
        if actions == ["no-op"]:
            continue
        action = "replace" if len(actions) == 2 else actions[0]
        counts[action] += 1
        if "delete" in actions:
            destructive.append(address)
        after = change.get("after") or {}
        unknown = change.get("after_unknown") or {}
        sensitive = change.get("after_sensitive") or {}
        require(
            after.get("region", "ap-northeast-2") == "ap-northeast-2"
            and not after.get("replica")
            and not unknown.get("region"),
            "PLAN_REGION",
        )
        if kind.startswith("aws_iam_"):
            require(action not in ("delete", "replace"), "IAM_DESTRUCTIVE")
            require(kind in ("aws_iam_role", "aws_iam_role_policy"), "IAM_RESOURCE")
            role = after
            role_address = address if kind == "aws_iam_role" else configured_roles.get(address)
            if kind == "aws_iam_role_policy":
                role = roles.get(after.get("role")) or roles_by_address.get(role_address or "")
            if not isinstance(role, dict):
                raise PolicyViolation("IAM_ROLE_UNKNOWN")
            path = role.get("path")
            require(path in ("/ddak/app/", "/ddak/pipeline/"), "IAM_ROLE_PATH")
            require(not update or path == "/ddak/app/", "IAM_UPDATE_OUTSIDE_APP")
            if path == "/ddak/app/":
                require(role.get("permissions_boundary") == boundary_arn, "IAM_BOUNDARY_REQUIRED")
            else:
                require(
                    role.get("name") == "ddak-codebuild"
                    and build_boundary_arn is not None
                    and role.get("permissions_boundary") == build_boundary_arn,
                    "BUILD_BOUNDARY_REQUIRED",
                )
            field = "assume_role_policy" if kind == "aws_iam_role" else "policy"
            require(unknown is not True and not unknown.get(field), "IAM_POLICY_UNKNOWN")
            require(sensitive is not True and not sensitive.get(field), "IAM_POLICY_SENSITIVE")
            before_sensitive = change.get("before_sensitive") or {}
            require(
                before_sensitive is not True and not before_sensitive.get(field),
                "IAM_POLICY_SENSITIVE",
            )
            policy = policy_json(after.get(field))
            inspect_policy(
                policy,
                trust=kind == "aws_iam_role",
                account=account_id,
                platform=path == "/ddak/pipeline/",
            )
            request = {
                "policyDocument": json.dumps(policy),
                "locale": "KO",
                "policyType": "RESOURCE_POLICY" if kind == "aws_iam_role" else "IDENTITY_POLICY",
            }
            if kind == "aws_iam_role":
                request["validatePolicyResourceType"] = "AWS::IAM::AssumeRolePolicyDocument"
            seen_tokens = set()
            while True:
                response = analyzer.validate_policy(**request)
                require(isinstance(response.get("findings"), list), "ANALYZER_RESULT")
                errors += sum(f["findingType"] == "ERROR" for f in response["findings"])
                warnings += sum(
                    f["findingType"] == "SECURITY_WARNING" for f in response["findings"]
                )
                token = response.get("nextToken")
                if not token:
                    break
                require(token not in seen_tokens and len(seen_tokens) < 100, "ANALYZER_PAGINATION")
                seen_tokens.add(token)
                request["nextToken"] = token
            added = []
            if kind == "aws_iam_role_policy":
                # 전체 허용문을 명시한다. 이전 정책과의 차분으로 오인하지 않도록 키 이름도 명시.
                for stmt in statements(policy):
                    if stmt["Effect"] == "Allow":
                        resources = strings(stmt.get("Resource"))
                        require(
                            all(r.startswith("arn:aws:") and "${" not in r for r in resources),
                            "IAM_RESOURCE_UNKNOWN",
                        )
                        if "secretsmanager:getsecretvalue" in {
                            a.lower() for a in strings(stmt["Action"])
                        }:
                            prefix = f"arn:aws:secretsmanager:ap-northeast-2:{account_id}:secret:"
                            for ref in resources:
                                suffix = ref.removeprefix(prefix)
                                app_pattern = (
                                    re.escape(f"ddak/{project}/")
                                    + r"[A-Z][A-Z0-9_]*-(?:\?{6}|[A-Za-z0-9]{6})"
                                )
                                own_app = path == "/ddak/app/" and bool(
                                    re.fullmatch(app_pattern, suffix)
                                )
                                pull = (
                                    path == "/ddak/app/"
                                    and suffix == "ddak-platform/dockerhub-pull-??????"
                                )
                                build = (
                                    path == "/ddak/pipeline/"
                                    and role.get("name") == "ddak-codebuild"
                                    and suffix == "ddak-platform/dockerhub-push-??????"
                                )
                                dbinit = (
                                    path == "/ddak/app/"
                                    and (
                                        role_address == "aws_iam_role.dbinit_execution"
                                        or role.get("name") == f"ddak-{project}-dbinit-exec"
                                    )
                                    and (
                                        ref == rds_master_secret_arn
                                        or (
                                            not update
                                            and bool(
                                                re.fullmatch(
                                                    re.escape(prefix)
                                                    + (
                                                        r"rds!db-\?{8}-\?{4}-\?{4}-"
                                                        r"\?{4}-\?{12}-\?{6}"
                                                    ),
                                                    ref,
                                                )
                                            )
                                        )
                                    )
                                )
                                require(
                                    ref.startswith(prefix) and (own_app or pull or build or dbinit),
                                    "ROLE_SECRET_SCOPE",
                                )
                        added.append(
                            {
                                "actions": strings(stmt["Action"]),
                                "resources": [_masked(r) for r in resources],
                            }
                        )
            iam_diff.append(
                {
                    "address": address,
                    "action": action,
                    "role_path": path,
                    "boundary_attached": role.get("permissions_boundary")
                    == (build_boundary_arn if path == "/ddak/pipeline/" else boundary_arn),
                    "trust_changed": kind == "aws_iam_role",
                    "proposed_allow": added,
                    "before": _policy_view(policy_json(change["before"][field]))
                    if change.get("before") and change["before"].get(field)
                    else [],
                    "after": _policy_view(policy),
                }
            )
    require(not update or not destructive, "UPDATE_DESTRUCTIVE")
    require(errors == 0, "ACCESS_ANALYZER_ERROR")
    require(checkov.get("passed") is True, "CHECKOV_FAILED")
    result = {
        "layer": layer,
        "plan_sha256": plan_sha256,
        "exit_code": exit_code,
        "headline": (
            f"생성 {counts['create']} · 수정 {counts['update']} · "
            f"삭제 {counts['delete']} · 교체 {counts['replace']}"
        ),
        "counts": counts,
        "destructive": destructive,
        "iam_diff": iam_diff,
        "access_analyzer": {"errors": errors, "security_warnings": warnings},
        "checkov": checkov,
        "sensitive_masked": True,
    }
    require(
        len(json.dumps(result, ensure_ascii=False, sort_keys=True).encode()) <= 8192,
        "SUMMARY_TOO_LARGE",
    )
    return result


def filter_outputs(raw: dict[str, Any], allowed: dict[str, str]) -> dict[str, Any]:
    """이름과 예상 타입을 코드가 제공한다. sensitive/없는 출력은 갱신을 실패시킨다."""
    require(isinstance(raw, dict), "OUTPUT_FORMAT")
    result: dict[str, Any] = {}
    for name, kind in allowed.items():
        item = raw.get(name)
        if not isinstance(item, dict) or item.get("sensitive") is not False:
            raise PolicyViolation("OUTPUT_MISSING_OR_SENSITIVE")
        value = item.get("value")
        if kind == "string":
            require(isinstance(value, str) and bool(value), "OUTPUT_TYPE")
        elif kind == "list(string)":
            require(
                isinstance(value, list) and all(isinstance(v, str) for v in value), "OUTPUT_TYPE"
            )
        elif kind == "map(string)":
            require(
                isinstance(value, dict) and all(isinstance(v, str) for v in value.values()),
                "OUTPUT_TYPE",
            )
        else:
            raise PolicyViolation("OUTPUT_TYPE")
        result[name] = value
    return result
