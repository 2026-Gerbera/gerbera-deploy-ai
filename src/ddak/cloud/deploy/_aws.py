"""cloud/deploy 공용: AWS 호출 오류를 원문 없이 감싼다."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ddak.core.contracts.errors import DdakToolError, ErrorCode


def call(message: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return fn()
    except Exception as exc:  # botocore ClientError 등. 원문에는 ARN·계정 ID가 섞인다
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, message) from exc
