"""최초 플랫폼 적용 직후 Docker Hub 자격증명을 Secrets Manager에 채운다."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

import boto3
from botocore.config import Config

from ddak.core.aws_credentials import checked_session
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode

_REGION = "ap-northeast-2"


def seed_registry_secrets(ctx: RunContext) -> None:
    """호스트 환경의 값을 새 플랫폼 시크릿에 기록한다. 값은 반환하거나 로그하지 않는다."""
    if ctx.adapter_mode is not AdapterMode.REAL:
        return
    cloud = ctx.platform.get("cloud")
    if not isinstance(cloud, Mapping):
        raise DdakToolError(ErrorCode.INFRA_MISSING, "Docker Hub 시크릿 출력이 없다")
    user = os.environ.get("DDAK_DOCKERHUB_USER")
    push = os.environ.get("DDAK_DOCKERHUB_PUSH_TOKEN")
    pull = os.environ.get("DDAK_DOCKERHUB_PULL_TOKEN")
    if not all((user, push, pull)):
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "DDAK_DOCKERHUB_USER/PUSH_TOKEN/PULL_TOKEN 설정이 필요하다",
            needs_human=True,
        )
    identifiers = {
        "push": cloud.get("dockerhub_push_secret_arn"),
        "pull": cloud.get("dockerhub_pull_secret_arn"),
    }
    if not all(isinstance(value, str) and value for value in identifiers.values()):
        raise DdakToolError(ErrorCode.INFRA_MISSING, "Docker Hub 시크릿 ARN 출력이 없다")
    try:
        config = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2})
        client = checked_session(
            ctx.project_settings, region_name=_REGION, config=config, session_factory=boto3.Session
        ).client("secretsmanager", config=config)
        values: dict[str, Any] = {"push": push, "pull": pull}
        for kind, secret_id in identifiers.items():
            client.put_secret_value(
                SecretId=secret_id,
                SecretString=json.dumps(
                    {"username": user, "password": values[kind]}, separators=(",", ":")
                ),
            )
    except DdakToolError:
        raise
    except Exception:
        raise DdakToolError(
            ErrorCode.ADAPTER_FAILED,
            "Docker Hub 자격증명을 Secrets Manager에 기록하지 못했다",
            needs_human=True,
        ) from None
