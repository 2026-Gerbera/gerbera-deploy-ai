"""FAKE 앱 조립 전용 인프라 번들. SDK/CLI 경계만 결정적 fixture로 대체한다."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from botocore.exceptions import ClientError
from hcl2 import loads

from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.project_settings import cloud_platform_name

from .bindings import InfraBinding
from .runtime import AwsSettings, CommandResult, InfraRuntime, SessionKeys

_ACCOUNT = "123456789012"


class _FixtureSession:
    def __init__(self, **kwargs):
        self.keys = SimpleNamespace(
            access_key=kwargs.get("aws_access_key_id", "fixture-access"),
            secret_key=kwargs.get("aws_secret_access_key", "fixture-value"),
            token=kwargs.get("aws_session_token", "fixture-session"),
        )

    def get_credentials(self):
        return SimpleNamespace(get_frozen_credentials=lambda: self.keys)

    def client(self, name, **kwargs):
        return _FixtureSDK()


class _FixtureSDK:
    """최초 생성 시나리오만. 실제 client/session 생성 경로를 갖지 않는다."""

    def __getattr__(self, name: str):
        if name not in {
            "get_caller_identity",
            "head_bucket",
            "head_object",
            "get_policy",
            "get_role",
            "create_role",
            "attach_role_policy",
            "create_bucket",
            "create_policy",
            "put_bucket_tagging",
            "put_public_access_block",
            "put_bucket_policy",
            "put_bucket_versioning",
            "put_bucket_encryption",
            "put_object",
            "delete_object",
        }:
            raise AttributeError(name)

        def call(**kwargs):
            if name in {"head_bucket", "head_object", "get_policy", "get_role"}:
                code = "NoSuchEntity" if name in {"get_policy", "get_role"} else "404"
                raise ClientError({"Error": {"Code": code}}, name)
            if name == "get_caller_identity":
                return {"Account": _ACCOUNT}
            if name == "create_policy":
                return {
                    "Policy": {
                        "Arn": (
                            f"arn:aws:iam::{_ACCOUNT}:policy{kwargs['Path']}{kwargs['PolicyName']}"
                        ),
                        "DefaultVersionId": "v1",
                    }
                }
            return {"ETag": "fixture-etag"}

        return call


class _FixtureRunner:
    def __init__(self, source: str, outputs: dict[str, Any]):
        self.outputs = outputs
        # fixture plan의 after는 HCL 보간식 대신 확정된 식별자를 반환한다.
        resolved = source.replace("${var.account_id}", _ACCOUNT)
        for expression, arn in (
            ("aws_lb_target_group.fixture.arn", "targetgroup/fixture/abc"),
            ("aws_lb_target_group.alternate.arn", "targetgroup/alternate/def"),
            ("aws_lb_listener_rule.fixture.arn", "listener-rule/app/fixture/abc/def"),
        ):
            resolved = resolved.replace(
                expression,
                json.dumps(f"arn:aws:elasticloadbalancing:ap-northeast-2:{_ACCOUNT}:{arn}"),
            )
        self.raw = {
            "format_version": "1.2",
            "resource_changes": [
                {
                    "type": resource,
                    "mode": "managed",
                    "address": f"{resource}.{name}",
                    "change": {
                        "actions": ["create"],
                        "after": attributes,
                        "after_unknown": {},
                        "after_sensitive": {},
                    },
                }
                for item in loads(resolved)["resource"]
                for resource, named in item.items()
                for name, attributes in named.items()
            ],
        }

    def run(self, argv, *, cwd, deadline, session=None):
        verb = argv[1]
        if Path(argv[0]).name == "checkov":
            return CommandResult(
                0, '{"results":{"failed_checks":[],"skipped_checks":[],"passed_checks":[]}}'
            )
        if verb == "plan":
            (cwd / "approved.tfplan").write_bytes(json.dumps(self.raw, sort_keys=True).encode())
            return CommandResult(2, "")
        if verb == "show":
            return CommandResult(0, json.dumps(self.raw))
        if verb == "validate":
            return CommandResult(0, '{"valid":true}')
        if verb == "output":
            return CommandResult(
                0,
                json.dumps({k: {"value": v, "sensitive": False} for k, v in self.outputs.items()}),
            )
        if verb not in {"fmt", "init", "apply"}:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "fixture에 없는 인프라 명령")
        return CommandResult(0, "source=fixture")


def fixture_binding(ctx: RunContext, *, root: Path, approvals: Any, guard: Any) -> InfraBinding:
    """일반 runtime의 plan 해시·승인·잠금 검사는 유지한다. REAL에서 호출하면 거부한다."""
    if ctx.adapter_mode is not AdapterMode.FAKE:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "fixture 인프라는 FAKE 모드 전용이다")
    if (ctx.project_settings.get("_infra_storage") or {}).get("intent") in {"create", "remove"}:
        from .storage_fixture import storage_fixture_binding

        return storage_fixture_binding(ctx, root=root, approvals=approvals, guard=guard)
    layer = "platform" if ctx.mode is RunMode.BOOTSTRAP else "app"
    resource = "aws_ecs_cluster" if layer == "platform" else "aws_secretsmanager_secret"
    platform = cloud_platform_name(ctx.project, ctx.project_settings)
    name = f"ddak-{platform}-fixture" if layer == "platform" else f"ddak/{platform}/FIXTURE"
    source = f'resource "{resource}" "fixture" {{ name = "{name}" }}\n'
    if layer == "platform":
        source += """
resource "aws_ecs_service" "fixture" {
  name = "ddak-fixture-service"
  cluster = aws_ecs_cluster.fixture.id
  lifecycle { ignore_changes = [task_definition, desired_count] }
  deployment_circuit_breaker { enable = true
    rollback = true }
  deployment_configuration { strategy = "BLUE_GREEN"
    bake_time_in_minutes = 1 }
  load_balancer {
    target_group_arn = aws_lb_target_group.fixture.arn
    container_name = "web"
    container_port = 8080
    advanced_configuration {
      alternate_target_group_arn = aws_lb_target_group.alternate.arn
      production_listener_rule = aws_lb_listener_rule.fixture.arn
      role_arn = "arn:aws:iam::${var.account_id}:role/ddak/infra/ddak-ecs-infra-elb"
    }
  }
}
resource "aws_lb_target_group" "fixture" {
  name = "ddak-fixture-target"
  deregistration_delay = 5
  health_check { interval = 5
    healthy_threshold = 2 }
}
resource "aws_lb_target_group" "alternate" {
  name = "ddak-fixture-alternate"
  deregistration_delay = 5
  health_check { interval = 5
    healthy_threshold = 2 }
}
resource "aws_lb_listener_rule" "fixture" {
  lifecycle { ignore_changes = [action] }
  action {
    type = "forward"
    forward {
      target_group { arn = aws_lb_target_group.fixture.arn
        weight = 100 }
      target_group { arn = aws_lb_target_group.alternate.arn
        weight = 0 }
    }
  }
}
resource "aws_security_group" "fixture" { name = "ddak-fixture-app" }
resource "aws_subnet" "fixture" { cidr_block = "10.0.1.0/24" }
"""
        declarations = {
            "cluster_name": ("aws_ecs_cluster.fixture.name", "string"),
            "ecs_service_name": ("aws_ecs_service.fixture.name", "string"),
            "target_group_arn": ("aws_lb_target_group.fixture.arn", "string"),
            "app_security_group_id": ("aws_security_group.fixture.id", "string"),
            "public_subnet_ids": ("[aws_subnet.fixture.id]", "list(string)"),
        }
        outputs = {
            "cluster_name": name,
            "ecs_service_name": "ddak-fixture-service",
            "target_group_arn": (
                f"arn:aws:elasticloadbalancing:ap-northeast-2:{_ACCOUNT}:targetgroup/fixture/abc"
            ),
            "app_security_group_id": "sg-fixture",
            "public_subnet_ids": ["subnet-fixture"],
        }
    else:
        declarations = {
            "app_secret_arn_FIXTURE": ("aws_secretsmanager_secret.fixture.arn", "string")
        }
        outputs = {
            "app_secret_arn_FIXTURE": (
                f"arn:aws:secretsmanager:ap-northeast-2:{_ACCOUNT}:secret:{name}-ABC123"
            )
        }
    runner: Any = _FixtureRunner(source, outputs)
    sdk = {service: _FixtureSDK() for service in ("s3", "iam", "sts")}
    session = SessionKeys("fixture-access", "fixture-value", "fixture-session")
    runtime = InfraRuntime(
        root=root,
        run_id=ctx.run_id,
        settings=AwsSettings(
            platform,
            _ACCOUNT,
            "ddak-fixture-state",
            layer,
            declarations,
            approval_project=ctx.project,
        ),
        lock_file=b"source=fixture\n",
        runner=runner,
        approvals=approvals,
        guard=guard,
        foundation_clients=lambda keys: sdk,
        aws_project_settings={"aws_profile": "fixture", "aws_expected_account_id": _ACCOUNT},
        session_factory=_FixtureSession,
    )
    return InfraBinding(
        runtime=runtime,
        files={"main.tf": source},
        mode=AdapterMode.FAKE,
        read_session=lambda: session,
        apply_session=lambda: session,
        analyzer=lambda: None,  # fixture에는 IAM policy resource가 없다.
        generation_source=Source.FIXTURE,
    )
