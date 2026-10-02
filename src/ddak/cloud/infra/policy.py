"""생성된 HCL을 init 전에 검사한다. 실패 원문 대신 고정된 규칙 ID만 반환한다."""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hcl2.api import loads, parses

from .providers.aws import APP_RESOURCE_TYPES, RESOURCE_TYPES

# StartBuild의 코드 소유 buildspecOverride가 빠지면 앱 저장소 buildspec을 읽지 않고 실패한다.
OVERRIDE_REQUIRED_BUILDSPEC = "version: 0.2\nphases:\n  build:\n    commands:\n      - exit 1\n"


def protect_platform_resource(kind: str, body: dict[str, Any], state_bucket: str | None) -> None:
    if kind.startswith("aws_s3_bucket") and state_bucket is not None:
        require(body.get("bucket") != state_bucket, "FOUNDATION_BUCKET_OWNED_BY_CODE")
    if kind == "aws_codebuild_project":
        source = body.get("source")
        require(isinstance(source, list) and len(source) == 1, "CODEBUILD_SOURCE_REQUIRED")
        require(
            source[0].get("buildspec") == OVERRIDE_REQUIRED_BUILDSPEC,
            "CODEBUILD_PLATFORM_BUILDSPEC_REQUIRED",
        )


@dataclass(frozen=True)
class GateResult:
    passed: bool
    detail: str = ""


class PolicyViolation(ValueError):
    """메시지는 입력값을 포함하지 않는 코드 소유 규칙 ID다."""


def require(condition: bool, rule: str) -> None:
    if not condition:
        raise PolicyViolation(rule)


def policy_json(value: Any) -> dict[str, Any]:
    require(isinstance(value, str), "IAM_POLICY_UNKNOWN")
    # python-hcl2가 jsonencode의 literal object를 JSON으로 변환한다.
    if value.startswith("${jsonencode(") and value.endswith(")}"):
        value = value[len("${jsonencode(") : -2]
    try:
        result = json.loads(value)
    except (ValueError, TypeError):
        raise PolicyViolation("IAM_POLICY_UNKNOWN") from None
    require(isinstance(result, dict), "IAM_POLICY_UNKNOWN")
    return result


def statements(policy: dict[str, Any]) -> list[dict[str, Any]]:
    result = policy.get("Statement")
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list) or not result:
        raise PolicyViolation("IAM_STATEMENTS_REQUIRED")
    require(all(isinstance(s, dict) for s in result), "IAM_STATEMENTS_REQUIRED")
    return result


def strings(value: Any) -> list[str]:
    result = [value] if isinstance(value, str) else value
    require(isinstance(result, list) and bool(result), "IAM_VALUE_UNKNOWN")
    require(all(isinstance(s, str) and bool(s) for s in result), "IAM_VALUE_UNKNOWN")
    return result


def inspect_policy(
    policy: dict[str, Any],
    *,
    trust: bool = False,
    account: str = "${var.account_id}",
    platform: bool = False,
) -> None:
    for stmt in statements(policy):
        require(not {"NotAction", "NotResource", "NotPrincipal"} & stmt.keys(), "IAM_NEGATED_RULE")
        require(stmt.get("Effect") in ("Allow", "Deny"), "IAM_EFFECT")
        actions = strings(stmt.get("Action"))
        if trust:
            services = {"ecs-tasks.amazonaws.com"}
            if platform:
                services = {"codebuild.amazonaws.com"}
            require(stmt.get("Effect") == "Allow", "IAM_TRUST_EFFECT")
            require(actions == ["sts:AssumeRole"], "IAM_TRUST_ACTION")
            principal = stmt.get("Principal", {})
            require(
                isinstance(principal, dict) and set(principal) == {"Service"}, "IAM_TRUST_PRINCIPAL"
            )
            require(set(strings(principal["Service"])) <= services, "IAM_TRUST_PRINCIPAL")
            condition = stmt.get("Condition", {})
            require(isinstance(condition, dict), "IAM_SOURCE_ACCOUNT")
            require(
                condition.get("StringEquals", {}).get("aws:SourceAccount") == account,
                "IAM_SOURCE_ACCOUNT",
            )
        elif stmt["Effect"] == "Allow":
            require(
                all("*" not in a and "?" not in a and ":" in a for a in actions),
                "IAM_WILDCARD_ACTION",
            )
            require("Principal" not in stmt, "IAM_IDENTITY_PRINCIPAL")
            resources = strings(stmt.get("Resource"))
            require(all(r != "*" for r in resources), "IAM_WILDCARD_RESOURCE")
            # 계획에서 확정할 수 없는 리소스 참조는 승인할 수 없다.
            require(
                all(not re.search(r"\$\{(?:aws_|module\.)", r) for r in resources),
                "IAM_COMPUTED_RESOURCE",
            )


def _walk(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def static_gate(
    files: Mapping[str, str], *, layer: str, state_bucket: str | None = None
) -> GateResult:
    """리소스 블록만 허용. 변수/provider/backend/outputs는 코드 소유 틀에서 주입한다."""
    try:
        require(layer in ("app", "platform") and bool(files), "BUNDLE_REQUIRED")
        require(
            len(files) <= 32 and sum(len(s.encode()) for s in files.values()) <= 1024 * 1024,
            "BUNDLE_SIZE",
        )
        addresses: set[str] = set()
        for name, source in files.items():
            require(bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*\.tf", name)), "BUNDLE_FILENAME")
            require(len(source.encode()) <= 256 * 1024, "BUNDLE_SIZE")
            require("checkov:skip" not in source.lower(), "CHECKOV_SKIP_FORBIDDEN")
            tree = parses(source)
            for node in tree.iter_subtrees():
                # hcl2의 heredoc은 내부 보간식을 AST로 펼치지 않는다.
                require(not str(node.data).startswith("heredoc"), "HEREDOC_NOT_ALLOWED")
                if str(node.data) == "function_call":
                    require(
                        str(node.children[0].children[0]) == "jsonencode", "FUNCTION_NOT_ALLOWED"
                    )
            data = loads(source)
            require(set(data) == {"resource"}, "RESOURCE_BLOCKS_ONLY")
            for item in data["resource"]:
                for kind, named in item.items():
                    allowed = APP_RESOURCE_TYPES if layer == "app" else RESOURCE_TYPES
                    require(kind in allowed, "RESOURCE_NOT_ALLOWED")
                    for label, body in named.items():
                        require(
                            bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", label)), "RESOURCE_LABEL"
                        )
                        address = f"{kind}.{label}"
                        require(address not in addresses, "DUPLICATE_RESOURCE")
                        addresses.add(address)
                        protect_platform_resource(kind, body, state_bucket)
                        _resource(kind, body, layer)
        require(bool(addresses), "BUNDLE_REQUIRED")
        return GateResult(True)
    except PolicyViolation as exc:
        return GateResult(False, str(exc))
    except Exception:
        # 파서 오류에는 HCL·비밀값·절대 경로가 포함될 수 있다.
        return GateResult(False, "HCL_INVALID")


def inspect_ecs(kind: str, body: dict[str, Any], *, hcl: bool = False) -> None:
    """ECS 배포 소유권. lifecycle은 plan의 after에 없으므로 HCL에서만 검사한다."""
    if kind == "aws_ecs_service":
        breaker = body.get("deployment_circuit_breaker")
        require(
            isinstance(breaker, list)
            and len(breaker) == 1
            and isinstance(breaker[0], dict)
            and breaker[0].get("enable") is True
            and breaker[0].get("rollback") is True,
            "ECS_CIRCUIT_BREAKER_REQUIRED",
        )
        if hcl:
            lifecycle = body.get("lifecycle")
            require(
                isinstance(lifecycle, list)
                and len(lifecycle) == 1
                and isinstance(lifecycle[0], dict),
                "ECS_DEPLOYMENT_OWNED_BY_C2",
            )
            ignored = lifecycle[0].get("ignore_changes")
            require(
                isinstance(ignored, list) and all(isinstance(v, str) for v in ignored),
                "ECS_DEPLOYMENT_OWNED_BY_C2",
            )
            names = {v.removeprefix("${").removesuffix("}") for v in ignored}
            require({"task_definition", "desired_count"} <= names, "ECS_DEPLOYMENT_OWNED_BY_C2")
    if kind == "aws_ecs_task_definition":
        encoded = body.get("container_definitions")
        if not isinstance(encoded, str):
            raise PolicyViolation("CONTAINER_DEFINITIONS_UNKNOWN")
        if encoded.startswith("${jsonencode(") and encoded.endswith(")}"):
            encoded = encoded[len("${jsonencode(") : -2]
        try:
            containers = json.loads(encoded)
        except (ValueError, TypeError):
            raise PolicyViolation("CONTAINER_DEFINITIONS_UNKNOWN") from None
        require(isinstance(containers, list) and bool(containers), "CONTAINER_DEFINITIONS_UNKNOWN")
        require(
            all(
                isinstance(c, dict)
                and not any(
                    k.lower() in {"environment", "environmentfiles", "secrets"} and v
                    for k, v in c.items()
                )
                for c in containers
            ),
            "CONTAINER_ENV_MANAGED_BY_C2",
        )
        names = [c.get("name") for c in containers]
        require(
            all(n in ("web", "was") for n in names) and len(set(names)) == len(names),
            "ECS_CONTAINER_NAME_MUST_MATCH_TIER",
        )


def _resource(kind: str, body: dict[str, Any], layer: str) -> None:
    forbidden = {
        "provisioner",
        "connection",
        "dynamic",
        "provider",
        "for_each",
        "count",
        "password",
        "password_wo",
        "master_password",
        "secret_string",
        "secret_string_wo",
        "secret_binary",
        "access_key",
        "secret_key",
        "token",
        "environment_variable",
        "region",
        "replica",
    }
    for key, value in _walk(body):
        require(key not in forbidden, "ATTRIBUTE_NOT_ALLOWED")
        if isinstance(value, str):
            require(
                not re.search(r"\b(?:path|terraform|data|module)\.", value),
                "REFERENCE_NOT_ALLOWED",
            )
    if kind == "aws_iam_role":
        path = body.get("path")
        require(
            path == "/ddak/app/" or (layer == "platform" and path == "/ddak/pipeline/"),
            "IAM_ROLE_PATH",
        )
        build = path == "/ddak/pipeline/"
        if build:
            require(body.get("name") == "ddak-codebuild", "PLATFORM_ROLE_UNSUPPORTED")
        require(
            body.get("permissions_boundary")
            == ("${var.build_boundary_arn}" if build else "${var.app_boundary_arn}"),
            "IAM_BOUNDARY_REQUIRED",
        )
        inspect_policy(policy_json(body.get("assume_role_policy")), trust=True, platform=build)
        require(
            "inline_policy" not in body and "managed_policy_arns" not in body, "IAM_INLINE_SEPARATE"
        )
    if kind == "aws_iam_role_policy":
        inspect_policy(policy_json(body.get("policy")))
    if kind == "aws_secretsmanager_secret":
        name = body.get("name", "")
        require(
            isinstance(name, str)
            and (
                bool(re.fullmatch(r"ddak/\$\{var\.project\}/[A-Z][A-Z0-9_]*", name))
                or (
                    layer == "platform"
                    and name in ("ddak-platform/dockerhub-pull", "ddak-platform/dockerhub-push")
                )
            ),
            "SECRET_NAME_SCOPE",
        )
        require("policy" not in body, "SECRET_RESOURCE_POLICY_FORBIDDEN")
    inspect_ecs(kind, body, hcl=True)
    if kind == "aws_db_instance":
        require(body.get("manage_master_user_password") is True, "RDS_MANAGED_PASSWORD")
        require(body.get("publicly_accessible") is False, "RDS_PRIVATE")
    if kind in {"aws_security_group", "aws_vpc_security_group_ingress_rule"}:
        rules = body.get("ingress", []) if kind == "aws_security_group" else [body]
        for rule in rules:
            # 미확정 포트·CIDR은 안전하다고 가정하지 않는다.
            cidrs = rule.get("cidr_blocks", []) + rule.get("ipv6_cidr_blocks", [])
            cidrs += [rule.get("cidr_ipv4"), rule.get("cidr_ipv6")]
            for cidr in (c for c in cidrs if c is not None):
                try:
                    ipaddress.ip_network(cidr, strict=False)
                except (ValueError, TypeError):
                    raise PolicyViolation("CIDR_MUST_BE_LITERAL") from None
            if any(c is not None for c in cidrs):
                low, high = rule.get("from_port"), rule.get("to_port")
                if type(low) is not int or type(high) is not int:
                    raise PolicyViolation("PORT_MUST_BE_LITERAL")
                if low <= 3306 <= high or rule.get("protocol", rule.get("ip_protocol")) == "-1":
                    require(all(_private_cidr(c) for c in cidrs if c is not None), "MYSQL_PUBLIC")


def _private_cidr(cidr: str) -> bool:
    network = ipaddress.ip_network(cidr, strict=False)
    if isinstance(network, ipaddress.IPv4Network):
        return any(
            network.subnet_of(ipaddress.IPv4Network(n))
            for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
    return network.subnet_of(ipaddress.IPv6Network("fc00::/7"))
