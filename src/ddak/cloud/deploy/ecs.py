"""cloud/deploy/ecs.py: ECS Fargate 배포·롤백. 담당 안승환(C2).

태스크 정의 새 리비전(이미지 = RunContext.images의 digest 고정 참조, secrets valueFrom = 인프라
출력의 시크릿 ARN) -> update_service. boto3는 호출마다 새 Session. AI import 금지.

내부 API(replace_image)는 클러스터·서비스·컨테이너 이름을 인자로 받는다. ctx.platform['cloud']의
Terraform 출력 이름이 정해지면 deploy_service/rollback_service가 그 값을 읽어 부른다(💭 확정 필요).

- 새 리비전은 현재 서비스의 태스크 정의를 그대로 복사하고 지정한 컨테이너의 image만 바꾼다.
  시크릿(valueFrom)·Docker Hub repositoryCredentials·역할은 Terraform이 만든 값을 유지한다.
- 이미지는 digest 고정 참조만 받는다. 태그로 배포하지 않는다.
- 롤백도 같은 경로다(이전 릴리스 이미지로 새 리비전). 이전 리비전 ARN에 기대지 않는다.
- 안정화: 새 리비전의 PRIMARY 배포가 rolloutState COMPLETED이고 이전 배포가 모두 빠지면 성공,
  FAILED(배포 회로 차단기)면 실패. deadline(monotonic)을 넘기면 시간 초과.
- 오류 메시지에 ARN·계정 ID·AWS 오류 원문을 넣지 않는다.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ddak.cloud.deploy._aws import call
from ddak.core.contracts.errors import DdakToolError, ErrorCode

_IMAGE_REF = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
# describe_task_definition 결과 중 register_task_definition에 다시 넣을 수 있는 키
_REGISTER_KEYS = (
    "family",
    "taskRoleArn",
    "executionRoleArn",
    "networkMode",
    "containerDefinitions",
    "volumes",
    "placementConstraints",
    "requiresCompatibilities",
    "cpu",
    "memory",
    "pidMode",
    "ipcMode",
    "proxyConfiguration",
    "inferenceAccelerators",
    "ephemeralStorage",
    "runtimePlatform",
    "enableFaultInjection",
)


class EcsClient(Protocol):
    def describe_services(self, **kwargs: Any) -> dict[str, Any]: ...

    def describe_task_definition(self, **kwargs: Any) -> dict[str, Any]: ...

    def register_task_definition(self, **kwargs: Any) -> dict[str, Any]: ...

    def update_service(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_tasks(self, **kwargs: Any) -> dict[str, Any]: ...

    def describe_tasks(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class EcsService:
    cluster: str
    service: str
    container: str  # 태스크 정의 안에서 이미지를 바꿀 컨테이너 이름


@dataclass(frozen=True)
class Revision:
    task_definition: str  # image_ref를 쓰는 리비전 ARN(이미 같은 image면 현재 리비전)
    previous_task_definition: str
    previous_image: str | None  # 바꾸기 전 컨테이너 image(태그 참조일 수도 있다)
    changed: bool  # 이미 같은 image였으면 False(새 리비전·update_service 없음)


def register_revision(client: EcsClient, target: EcsService, image_ref: str) -> Revision:
    """서비스의 현재 태스크 정의를 복사해 컨테이너 image만 바꾼 새 리비전을 등록한다.

    서비스는 바꾸지 않는다. 마이그레이션(database.py)이 서비스 교체 전에 이 리비전으로 돈다.
    """
    if not _IMAGE_REF.fullmatch(image_ref):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "이미지는 digest 고정 참조여야 한다")
    current_arn = _service(client, target)["taskDefinition"]
    described = call(
        "태스크 정의를 읽지 못했다",
        lambda: client.describe_task_definition(taskDefinition=current_arn, include=["TAGS"]),
    )
    definition = described["taskDefinition"]
    containers = [dict(c) for c in definition.get("containerDefinitions") or []]
    matched = [c for c in containers if c.get("name") == target.container]
    if len(matched) != 1:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "태스크 정의에 대상 컨테이너가 없다")
    previous_image = matched[0].get("image")
    if previous_image == image_ref:
        return Revision(current_arn, current_arn, previous_image, changed=False)
    matched[0]["image"] = image_ref

    register: dict[str, Any] = {k: definition[k] for k in _REGISTER_KEYS if k in definition}
    register["containerDefinitions"] = containers
    if described.get("tags"):
        register["tags"] = described["tags"]
    new_arn = call(
        "태스크 정의 새 리비전을 등록하지 못했다",
        lambda: client.register_task_definition(**register),
    )["taskDefinition"]["taskDefinitionArn"]
    return Revision(new_arn, current_arn, previous_image, changed=True)


def replace_image(
    client: EcsClient,
    target: EcsService,
    image_ref: str,
    deadline: float,
    *,
    poll_s: float = 10.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Revision:
    """서비스의 컨테이너 image를 image_ref로 바꾼 새 리비전을 배포하고 안정화까지 기다린다."""
    revision = register_revision(client, target, image_ref)
    if not revision.changed:
        return revision
    call(
        "ECS 서비스를 바꾸지 못했다",
        lambda: client.update_service(
            cluster=target.cluster,
            service=target.service,
            taskDefinition=revision.task_definition,
        ),
    )
    wait_stable(
        client, target, revision.task_definition, deadline, poll_s=poll_s, clock=clock, sleep=sleep
    )
    return revision


def wait_stable(
    client: EcsClient,
    target: EcsService,
    task_definition: str,
    deadline: float,
    *,
    poll_s: float = 10.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """새 리비전 배포가 끝날 때까지 읽기 폴링한다."""
    while True:
        deployments = _service(client, target).get("deployments") or []
        primary = [d for d in deployments if d.get("status") == "PRIMARY"]
        if len(primary) != 1 or primary[0].get("taskDefinition") != task_definition:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "ECS 서비스가 다른 리비전으로 바뀌었다")
        state = primary[0].get("rolloutState")
        if state == "FAILED":
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "ECS 배포가 실패했다(회로 차단기)")
        if state == "COMPLETED" and len(deployments) == 1:
            return
        remaining = deadline - clock()
        if remaining <= 0:
            raise DdakToolError(
                ErrorCode.ADAPTER_TIMEOUT, "ECS 배포가 제한 시간 안에 끝나지 않았다"
            )
        sleep(min(poll_s, remaining))


_ARCH = {"X86_64": "linux/amd64", "ARM64": "linux/arm64"}


@dataclass(frozen=True)
class Running:
    platform: str  # linux/amd64 | linux/arm64 (태스크 정의 runtimePlatform)
    image_digests: frozenset[str]  # 새 리비전으로 실행 중인 대상 컨테이너의 imageDigest


def running_image(client: EcsClient, target: EcsService, task_definition: str) -> Running:
    """새 리비전으로 실행 중인 태스크의 플랫폼과 실제 이미지 digest를 읽는다(관측값 원천)."""
    definition = call(
        "태스크 정의를 읽지 못했다",
        lambda: client.describe_task_definition(taskDefinition=task_definition),
    )["taskDefinition"]
    arch = (definition.get("runtimePlatform") or {}).get("cpuArchitecture") or "X86_64"
    if arch not in _ARCH:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "지원하지 않는 CPU 아키텍처다")
    arns = (
        call(
            "실행 중인 ECS 태스크를 읽지 못했다",
            lambda: client.list_tasks(
                cluster=target.cluster, serviceName=target.service, desiredStatus="RUNNING"
            ),
        ).get("taskArns")
        or []
    )
    tasks = (
        call(
            "실행 중인 ECS 태스크를 읽지 못했다",
            lambda: client.describe_tasks(cluster=target.cluster, tasks=arns),
        ).get("tasks")
        or []
        if arns
        else []
    )
    digests = {
        c.get("imageDigest")
        for t in tasks
        if t.get("taskDefinitionArn") == task_definition
        for c in t.get("containers") or []
        if c.get("name") == target.container
    }
    if not digests or None in digests:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "새 리비전의 실행 중 이미지 digest가 없다")
    return Running(platform=_ARCH[arch], image_digests=frozenset(str(d) for d in digests))


def scale_to_zero(
    client: EcsClient,
    target: EcsService,
    deadline: float,
    *,
    poll_s: float = 10.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """이전 릴리스가 없는 첫 배포의 복구: 서비스 태스크를 0개로 줄인다. 바꿨으면 True."""
    if _service(client, target).get("desiredCount", 0) == 0:
        return False
    call(
        "ECS 서비스를 바꾸지 못했다",
        lambda: client.update_service(
            cluster=target.cluster, service=target.service, desiredCount=0
        ),
    )
    while True:
        if _service(client, target).get("runningCount", 0) == 0:
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            raise DdakToolError(
                ErrorCode.ADAPTER_TIMEOUT, "ECS 서비스가 제한 시간 안에 멈추지 않았다"
            )
        sleep(min(poll_s, remaining))


def _service(client: EcsClient, target: EcsService) -> Mapping[str, Any]:
    response = call(
        "ECS 서비스를 읽지 못했다",
        lambda: client.describe_services(cluster=target.cluster, services=[target.service]),
    )
    services = [s for s in response.get("services") or [] if s.get("status") == "ACTIVE"]
    if len(services) != 1:
        raise DdakToolError(ErrorCode.INFRA_MISSING, "ECS 서비스를 찾지 못했다")
    return services[0]
