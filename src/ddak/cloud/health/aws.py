"""C3 읽기 전용 AWS 연결 도우미."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode


def cloud_platform(ctx: RunContext) -> Mapping[str, Any]:
    cloud = ctx.platform.get("cloud", ctx.platform)
    if not isinstance(cloud, Mapping):
        raise DdakToolError(ErrorCode.INFRA_MISSING, "클라우드 환경 정보가 없다")
    return cloud


def required(platform: Mapping[str, Any], key: str) -> str:
    value = platform.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DdakToolError(ErrorCode.INFRA_MISSING, f"클라우드 환경 정보에 {key}가 없다")
    return value


def session() -> boto3.Session:
    """쓰기 프로필로 묵시적으로 떨어지지 않는 검증 전용 세션."""
    profile = os.environ.get("DDAK_AWS_READONLY_PROFILE", "ddak-readonly")
    return boto3.Session(profile_name=profile)


def client(service: str, ctx: RunContext, region: str) -> Any:
    timeout = max(1, int(remaining(ctx)))
    config = Config(connect_timeout=timeout, read_timeout=timeout, retries={"max_attempts": 1})
    try:
        return session().client(service, region_name=region, config=config)
    except BotoCoreError as exc:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "AWS 읽기 전용 세션 생성 실패") from exc


def remaining(ctx: RunContext, default: float = 15.0) -> float:
    if ctx.deadline is None:
        return default
    value = ctx.deadline - time.monotonic()
    if value <= 0:
        raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "클라우드 검증 제한 시간 초과")
    return min(default, value)
