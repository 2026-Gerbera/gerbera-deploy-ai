"""cloud/deploy 공용: AWS 세션·클라이언트와 오류 감싸기.

- 호출마다 새 boto3 Session(자격증명 파일 교체 반영). 프로필·리전은 표준 환경 변수
  AWS_PROFILE·AWS_REGION(harness/env.example)을 따르고, platform cloud의 region이 있으면 우선한다.
- FAKE 컨텍스트로 실제 AWS를 부르지 않는다(dispatch는 FAKE면 FakeProvider를 고른다).
- 오류 메시지에 ARN·계정 ID·AWS 오류 원문을 넣지 않는다.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Any

import boto3
from botocore.config import Config

from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode

DEFAULT_REGION = "ap-northeast-2"
DEFAULT_BUDGET_S = 600.0  # ctx.deadline이 없을 때(직접 호출) 상한


def call(message: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return fn()
    except Exception as exc:  # botocore ClientError 등. 원문에는 ARN·계정 ID가 섞인다
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, message) from exc


def client(service: str, ctx: RunContext) -> Any:
    """ctx용 AWS 클라이언트. 테스트는 이 함수를 바꿔 끼운다."""
    if ctx.adapter_mode is AdapterMode.FAKE:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "FAKE 실행은 실제 AWS를 부르지 않는다")
    platform = ctx.platform.get("cloud")
    configured = platform.get("region") if isinstance(platform, Mapping) else None
    name = configured or os.environ.get("AWS_REGION") or DEFAULT_REGION
    config = Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 3})
    try:
        return boto3.Session(region_name=name).client(service, config=config)  # pyright: ignore[reportUnknownMemberType]
    except Exception as exc:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "AWS 세션을 만들지 못했다") from exc


def deadline(ctx: RunContext) -> float:
    return ctx.deadline if ctx.deadline is not None else time.monotonic() + DEFAULT_BUDGET_S
