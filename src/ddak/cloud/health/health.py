"""ECS 실제 상태와 애플리케이션 readiness를 함께 확인한다."""

from __future__ import annotations

import http.client
import json
import ssl
import time
from collections.abc import Mapping
from typing import Any

from ddak.cd.interface import ProviderName, ProviderResult
from ddak.cloud.health.aws import client, cloud_platform, remaining, required
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.release import ImageArtifact

_MAX_ATTEMPTS = 3
_RETRY_WINDOW_S = 10.0
_RETRY_DELAY_S = 3.0


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
    """이번 빌드와 변경 없는 tier에서 이월한 artifact의 허용 digest를 합친다."""
    images: list[ImageArtifact] = []
    if ctx.release_artifacts is not None:
        images.extend(ctx.release_artifacts.images.values())
    previous = ctx.previous_release.get("cloud")
    if isinstance(previous, Mapping):
        stored = previous.get("artifacts")
        raw_images = stored.get("images") if isinstance(stored, Mapping) else None
        if isinstance(raw_images, Mapping):
            for raw in raw_images.values():
                try:
                    images.append(ImageArtifact.model_validate(raw))
                except (TypeError, ValueError):
                    continue
        sources = previous.get("image_sources")
        if isinstance(sources, Mapping):
            for source in sources.values():
                raw = source.get("artifact") if isinstance(source, Mapping) else None
                try:
                    images.append(ImageArtifact.model_validate(raw))
                except (TypeError, ValueError):
                    continue
    return {
        digest
        for image in images
        for digest in (image.index_digest, *image.platform_digests.values())
    }


def _check_once(
    ctx: RunContext, ecs: Any, elbv2: Any, platform: Mapping[str, Any]
) -> ProviderResult:
    cluster = required(platform, "cluster_name")
    service_name = required(platform, "ecs_service_name")
    target_group = required(platform, "target_group_arn")
    services = ecs.describe_services(cluster=cluster, services=[service_name]).get("services", [])
    if len(services) != 1 or not isinstance(services[0].get("taskDefinition"), str):
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "ECS 서비스 리비전을 확인하지 못했다")
    active_revision = services[0]["taskDefinition"]

    task_arns = ecs.list_tasks(
        cluster=cluster,
        serviceName=service_name,
        desiredStatus="RUNNING",
        maxResults=10,
    ).get("taskArns", [])
    tasks = (
        ecs.describe_tasks(cluster=cluster, tasks=task_arns).get("tasks", []) if task_arns else []
    )
    active_tasks = [task for task in tasks if task.get("taskDefinitionArn") == active_revision]
    observed = {
        container.get("imageDigest")
        for task in active_tasks
        for container in task.get("containers", [])
        if container.get("imageDigest")
    }
    expected = _expected_image_digests(ctx)
    digest_ok = bool(expected) and bool(observed) and observed <= expected

    target_health = elbv2.describe_target_health(TargetGroupArn=target_group).get(
        "TargetHealthDescriptions", []
    )
    alb_ok = any(row.get("TargetHealth", {}).get("State") == "healthy" for row in target_health)
    ready = _json_get(ctx.cloud_domain or "", "/health/ready", remaining(ctx))
    version = _json_get(ctx.cloud_domain or "", "/version", remaining(ctx))
    ready_ok = ready.get("status") in {"ok", "ready"} or ready.get("ready") is True
    release_ok = version.get("release_id") == ctx.run_id
    checks = {"alb": alb_ok, "ready": ready_ok, "release": release_ok, "digest": digest_ok}
    return ProviderResult(
        provider=ProviderName.AWS.value,
        function="health_check",
        passed=all(checks.values()),
        detail=", ".join(f"{name}={'pass' if ok else 'fail'}" for name, ok in checks.items()),
    )


def health_check(ctx: RunContext) -> ProviderResult:
    """ALB healthy, 앱 readiness/release, ECS 실제 digest를 모두 확인한다."""
    if not ctx.cloud_domain:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "cloud_domain이 설정되지 않았다",
        )

    platform = cloud_platform(ctx)
    region = str(platform.get("region") or "ap-northeast-2")
    ecs = client("ecs", ctx, region)
    elbv2 = client("elbv2", ctx, region)
    deadline = time.monotonic() + min(_RETRY_WINDOW_S, remaining(ctx))
    last: ProviderResult | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            last = _check_once(ctx, ecs, elbv2, platform)
            if last.passed:
                return last
        except DdakToolError as exc:
            if exc.code not in {ErrorCode.ADAPTER_FAILED, ErrorCode.ADAPTER_TIMEOUT}:
                raise
            if attempt == _MAX_ATTEMPTS - 1:
                raise
        delay = min(_RETRY_DELAY_S, deadline - time.monotonic())
        if attempt < _MAX_ATTEMPTS - 1 and delay > 0:
            time.sleep(delay)
        else:
            break
    return last or ProviderResult(
        provider=ProviderName.AWS.value,
        function="health_check",
        passed=False,
        detail="클라우드 상태를 확인하지 못했다",
    )
