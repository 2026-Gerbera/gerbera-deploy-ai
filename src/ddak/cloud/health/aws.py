"""C3 읽기 전용 AWS 연결 도우미."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import boto3
from botocore.config import Config

from ddak.core.aws_credentials import checked_session
from ddak.core.config import AdapterMode
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


def session(ctx: RunContext, *, region: str, config: Config) -> Any:
    """선택한 프로필과 기대 계정을 확인한 검증 세션."""
    if ctx.adapter_mode is AdapterMode.FAKE:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "FAKE 실행은 실제 AWS를 부르지 않는다")
    return checked_session(
        ctx.project_settings, region_name=region, config=config, session_factory=boto3.Session
    )


def client(service: str, ctx: RunContext, region: str) -> Any:
    timeout = max(1, int(remaining(ctx)))
    config = Config(connect_timeout=timeout, read_timeout=timeout, retries={"max_attempts": 1})
    try:
        return session(ctx, region=region, config=config).client(service, config=config)
    except DdakToolError:
        raise
    except Exception:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "AWS 검증 세션 생성 실패") from None


def remaining(ctx: RunContext, default: float = 15.0) -> float:
    if ctx.deadline is None:
        return default
    value = ctx.deadline - time.monotonic()
    if value <= 0:
        raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "클라우드 검증 제한 시간 초과")
    return min(default, value)
