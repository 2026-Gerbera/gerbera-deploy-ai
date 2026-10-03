"""ECS 실제 상태와 애플리케이션 readiness를 함께 확인한다."""

from __future__ import annotations

import http.client
import json
import ssl
from typing import Any

from ddak.cd.interface import ProviderName, ProviderResult
from ddak.cloud.health.aws import client, cloud_platform, remaining, required
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode


def _json_get(domain: str, path: str, timeout: float) -> dict[str, Any]:
    connection = http.client.HTTPSConnection(
        domain, 443, timeout=timeout, context=ssl.create_default_context()
    )
    try:
        connection.request("GET", path, headers={"User-Agent": "ddak-verifier/1"})
        response = connection.getresponse()
        if response.status != 200:
            raise DdakToolError(
                ErrorCode.ADAPTER_FAILED,
                "클라우드 앱 응답이 200이 아니다",
            )
        value = json.loads(response.read(65537))
    except (OSError, json.JSONDecodeError) as exc:
        raise DdakToolError(
            ErrorCode.ADAPTER_FAILED,
            "클라우드 앱 상태 조회 실패",
        ) from exc
    finally:
        connection.close()

    if not isinstance(value, dict):
        raise DdakToolError(
            ErrorCode.ADAPTER_FAILED,
            "클라우드 앱 상태 응답 형식 오류",
        )

    return value


def _expected_image_digests(ctx: RunContext) -> set[str]:
    artifacts = ctx.release_artifacts
    if artifacts is None:
        return set()

    return {image.index_digest for image in artifacts.images.values()}


def health_check(ctx: RunContext) -> ProviderResult:
    """ALB healthy, 앱 readiness/release, ECS 실제 digest를 모두 확인한다."""
    if not ctx.cloud_domain:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "cloud_domain이 설정되지 않았다",
        )

    platform = cloud_platform(ctx)
    region = str(platform.get("region") or "ap-northeast-2")
    cluster = required(platform, "cluster_name")
    service_name = required(platform, "ecs_service_name")
    target_group = required(platform, "target_group_arn")

    ecs = client("ecs", ctx, region)
    elbv2 = client("elbv2", ctx, region)

    task_arns = ecs.list_tasks(
        cluster=cluster,
        serviceName=service_name,
        desiredStatus="RUNNING",
        maxResults=10,
    ).get("taskArns", [])

    if not task_arns:
        return ProviderResult(
            provider=ProviderName.AWS.value,
            function="health_check",
            passed=False,
            detail="실행 중인 ECS 태스크가 없다",
        )

    tasks = ecs.describe_tasks(
        cluster=cluster,
        tasks=task_arns,
    ).get("tasks", [])

    observed = {
        container.get("imageDigest")
        for task in tasks
        for container in task.get("containers", [])
        if container.get("imageDigest")
    }

    expected = _expected_image_digests(ctx)

    digest_ok = bool(expected) and bool(observed) and observed <= expected

    target_health = elbv2.describe_target_health(TargetGroupArn=target_group).get(
        "TargetHealthDescriptions", []
    )

    alb_ok = any(row.get("TargetHealth", {}).get("State") == "healthy" for row in target_health)

    ready = _json_get(
        ctx.cloud_domain,
        "/health/ready",
        remaining(ctx),
    )

    version = _json_get(
        ctx.cloud_domain,
        "/version",
        remaining(ctx),
    )

    ready_ok = ready.get("status") in {"ok", "ready"} or ready.get("ready") is True

    release_ok = version.get("release_id") == ctx.run_id

    checks = {
        "alb": alb_ok,
        "ready": ready_ok,
        "release": release_ok,
        "digest": digest_ok,
    }

    summary = ", ".join(f"{name}={'pass' if ok else 'fail'}" for name, ok in checks.items())

    return ProviderResult(
        provider=ProviderName.AWS.value,
        function="health_check",
        passed=all(checks.values()),
        detail=summary,
    )
