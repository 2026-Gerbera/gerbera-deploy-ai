"""FAKE 앱 조립 전용 인프라 번들. SDK/CLI 경계만 결정적 fixture로 대체한다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode

from .bindings import InfraBinding
from .runtime import AwsSettings, CommandResult, InfraRuntime, SessionKeys

_ACCOUNT = "123456789012"


class _FixtureSDK:
    """최초 생성 시나리오만. 실제 client/session 생성 경로를 갖지 않는다."""

    def __getattr__(self, name: str):
        if name not in {
            "get_caller_identity",
            "head_bucket",
            "head_object",
            "get_policy",
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
            if name in {"head_bucket", "head_object", "get_policy"}:
                code = "NoSuchEntity" if name == "get_policy" else "404"
                raise ClientError({"Error": {"Code": code}}, name)
            if name == "get_caller_identity":
                return {"Account": _ACCOUNT}
            return {"ETag": "fixture-etag"}

        return call


class _FixtureRunner:
    def __init__(self, resource: str, attributes: dict[str, str]):
        self.raw = {
            "format_version": "1.2",
            "resource_changes": [
                {
                    "type": resource,
                    "mode": "managed",
                    "address": f"{resource}.fixture",
                    "change": {
                        "actions": ["create"],
                        "after": attributes,
                        "after_unknown": {},
                        "after_sensitive": {},
                    },
                }
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
            return CommandResult(0, "{}")
        if verb not in {"fmt", "init", "apply"}:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "fixture에 없는 인프라 명령")
        return CommandResult(0, "source=fixture")


def fixture_binding(ctx: RunContext, *, root: Path, approvals: Any, guard: Any) -> InfraBinding:
    """일반 runtime의 plan 해시·승인·잠금 검사는 유지한다. REAL에서 호출하면 거부한다."""
    if ctx.adapter_mode is not AdapterMode.FAKE:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "fixture 인프라는 FAKE 모드 전용이다")
    layer = "platform" if ctx.mode is RunMode.BOOTSTRAP else "app"
    resource = "aws_ecs_cluster" if layer == "platform" else "aws_secretsmanager_secret"
    name = f"ddak-{ctx.project}-fixture" if layer == "platform" else f"ddak/{ctx.project}/FIXTURE"
    runner: Any = _FixtureRunner(resource, {"name": name})
    sdk = {service: _FixtureSDK() for service in ("s3", "iam", "sts")}
    session = SessionKeys("fixture-access", "fixture-value", "fixture-session")
    runtime = InfraRuntime(
        root=root,
        run_id=ctx.run_id,
        settings=AwsSettings(ctx.project, _ACCOUNT, "ddak-fixture-state", layer, {}),
        lock_file=b"source=fixture\n",
        runner=runner,
        approvals=approvals,
        guard=guard,
        foundation_clients=lambda keys: sdk,
    )
    return InfraBinding(
        runtime=runtime,
        files={"main.tf": f'resource "{resource}" "fixture" {{ name = "{name}" }}\n'},
        mode=AdapterMode.FAKE,
        read_session=lambda: session,
        apply_session=lambda: session,
        analyzer=lambda: None,  # fixture에는 IAM policy resource가 없다.
        generation_source=Source.FIXTURE,
    )
