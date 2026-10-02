"""코드 소유 C1 출력 허용 목록. 야간 추가, 아침 C2/O2 검토 필요."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

IMAGE_REPOSITORY_PATTERN = r"[a-z0-9]+(?:[._-][a-z0-9]+)*/[a-z0-9]+(?:[._-][a-z0-9]+)*"

PLATFORM_OUTPUTS = MappingProxyType(
    {
        "vpc_id": "string",
        "cluster_arn": "string",
        "cluster_name": "string",
        "ecs_service_name": "string",
        "target_group_arn": "string",
        "app_security_group_id": "string",
        "alb_arn": "string",
        "alb_dns_name": "string",
        "alb_security_group_id": "string",
        "certificate_arn": "string",
        "https_listener_arn": "string",
        "http_listener_arn": "string",
        "rds_endpoint": "string",
        "rds_master_secret_arn": "string",
        "codebuild_project_name": "string",
        "image_repository": "string",  # 플랫폼 출력: Docker Hub namespace/repository
        "source_bucket": "string",
        "dockerhub_push_secret_arn": "string",
        "dockerhub_pull_secret_arn": "string",
        "private_subnet_ids": "list(string)",
        "public_subnet_ids": "list(string)",
        "task_execution_role_arn": "string",
        "task_role_arn": "string",
        "dbinit_execution_role_arn": "string",
        "app_secret_arn_SECRET_KEY": "string",
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
            if name == "image_repository" and not re.fullmatch(IMAGE_REPOSITORY_PATTERN, value):
                raise ValueError("이미지 저장소 출력 형식 오류")
            if (name.endswith("_arn") or APP_SECRET_OUTPUT.fullmatch(name)) and not re.fullmatch(
                r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:[A-Za-z0-9/_+=.@:!-]+", value
            ):
                raise ValueError("인프라 ARN 출력 형식 오류")
        elif not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ValueError("인프라 목록 출력 타입 오류")
        result[name] = value
    return result


def checked_cloud_outputs(values: Mapping[str, Any]) -> dict[str, Any]:
    """저장된 두 층의 출력을 재검증한다. region 등 조립 설정은 저장 출력이 아니다."""
    platform, app = {}, {}
    for name, value in values.items():
        if name in PLATFORM_OUTPUTS:
            platform[name] = value
        elif name in APP_OUTPUTS or APP_SECRET_OUTPUT.fullmatch(name):
            app[name] = value
        else:
            raise ValueError("허용되지 않은 클라우드 출력 이름")
    return {**checked_outputs(platform, "platform"), **checked_outputs(app, "app")}
