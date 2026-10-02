"""코드 소유 C1 출력 허용 목록. 야간 추가, 아침 C2/O2 검토 필요."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

PLATFORM_OUTPUTS = MappingProxyType(
    {
        "vpc_id": "string",
        "cluster_arn": "string",
        "cluster_name": "string",
        "alb_arn": "string",
        "alb_dns_name": "string",
        "alb_security_group_id": "string",
        "certificate_arn": "string",
        "https_listener_arn": "string",
        "http_listener_arn": "string",
        "rds_endpoint": "string",
        "rds_master_secret_arn": "string",
        "codebuild_project_name": "string",
        "source_bucket": "string",
        "dockerhub_push_secret_arn": "string",
        "dockerhub_pull_secret_arn": "string",
        "private_subnet_ids": "list(string)",
        "public_subnet_ids": "list(string)",
    }
)
APP_OUTPUTS = MappingProxyType(
    {
        "task_execution_role_arn": "string",
        "task_role_arn": "string",
        "dbinit_execution_role_arn": "string",
        "secret_arn": "string",  # 이전 내부 API 호환용. 새 선언은 app_secret_arn_<KEY> 사용.
    }
)
APP_SECRET_OUTPUT = re.compile(r"^app_secret_arn_[A-Z][A-Z0-9_]{0,63}$")


def output_kind(layer: str, name: str) -> str:
    if layer == "app":
        if APP_SECRET_OUTPUT.fullmatch(name):
            return "string"
        if name in APP_OUTPUTS:
            return APP_OUTPUTS[name]
    elif layer == "platform" and name in PLATFORM_OUTPUTS:
        return PLATFORM_OUTPUTS[name]
    raise ValueError("허용되지 않은 인프라 출력 이름/층")


def checked_outputs(values: Mapping[str, Any], layer: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in values.items():
        kind = output_kind(layer, name)
        if kind == "string":
            if not isinstance(value, str) or not value or any(c in value for c in "\r\n\0"):
                raise ValueError("인프라 출력 타입 오류")
            if (name.endswith("_arn") or APP_SECRET_OUTPUT.fullmatch(name)) and not re.fullmatch(
                r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:[A-Za-z0-9/_+=.@:!-]+", value
            ):
                raise ValueError("인프라 ARN 출력 형식 오류")
        elif not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ValueError("인프라 목록 출력 타입 오류")
        result[name] = value
    return result
