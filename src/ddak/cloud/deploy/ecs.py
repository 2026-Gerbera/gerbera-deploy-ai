"""cloud/deploy/ecs.py: ECS Fargate 배포·롤백. 담당 안승환(C2).

태스크 정의 새 리비전(이미지 = RunContext.images의 digest 고정 참조, secrets valueFrom = 인프라
출력의 시크릿 ARN) -> update_service. boto3는 호출마다 새 Session. AI import 금지.

- 배치(R9 (a), 10/2 C2 결정): 서비스 1개, 태스크 하나에 web·was 컨테이너. 그래서 이미지는
  {컨테이너: 참조}로 받아 바뀐 컨테이너를 한 리비전에 모아 한 번만 전환한다.
- 배포 방식(롤링/블루그린)은 Terraform이 서비스 deploymentConfiguration.strategy로 정한다. 이 모듈은
  어느 쪽이든 같은 호출(새 리비전, update_service, 완료 대기)이다. 팀 결정은 롤링(10/2 결정 4),
  블루그린은 검토 후보다.
- 새 리비전은 현재 서비스의 태스크 정의를 그대로 복사하고 지정한 컨테이너의 image만 바꾼다.
  시크릿(valueFrom)·Docker Hub repositoryCredentials·역할은 Terraform이 만든 값을 유지한다.
- Terraform은 서비스를 태스크 0개로 만든다. 첫 배포 때 desired_count로 올린다.
- 이미지는 digest 고정 참조만 받는다. 태그로 배포하지 않는다.
- 롤백도 같은 경로다(이전 릴리스 이미지로 새 리비전). 이전 리비전 ARN에 기대지 않는다.
- 완료: 새 리비전의 PRIMARY 배포가 rolloutState COMPLETED이고 이전 배포가 모두 빠지면 성공,
  FAILED면 실패. 블루그린이면 bake 시간 뒤 이전 배포가 정리될 때 완료된다(💭 실제 AWS 확인 필요).
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


@dataclass(frozen=True)
class Revision:
    task_definition: str  # 요청 이미지를 쓰는 리비전 ARN(이미 같으면 현재 리비전)
    previous_task_definition: str
    previous_images: Mapping[str, str | None]  # 바꾸기 전 컨테이너별 image
    changed: bool  # 이미 같은 image였으면 False(새 리비전 없음)


def register_revision(client: EcsClient, target: EcsService, images: Mapping[str, str]) -> Revision:
    """서비스의 현재 태스크 정의를 복사해 images({컨테이너: 참조})만 바꾼 새 리비전을 등록한다.

    서비스는 바꾸지 않는다. 마이그레이션(database.py)이 서비스 교체 전에 이 리비전으로 돈다.
    """
    if not images:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "바꿀 컨테이너 이미지가 없다")
    if any(not _IMAGE_REF.fullmatch(ref) for ref in images.values()):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "이미지는 digest 고정 참조여야 한다")
    current_arn = _service(client, target)["taskDefinition"]
    described = call(
        "태스크 정의를 읽지 못했다",
        lambda: client.describe_task_definition(taskDefinition=current_arn, include=["TAGS"]),
    )
    definition = described["taskDefinition"]
    containers = [dict(c) for c in definition.get("containerDefinitions") or []]
    by_name = {c.get("name"): c for c in containers}
    missing = sorted(set(images) - by_name.keys())
    if missing:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, f"태스크 정의에 컨테이너가 없다: {', '.join(missing)}"
        )
    previous = {name: by_name[name].get("image") for name in images}
    if all(previous[name] == ref for name, ref in images.items()):
        return Revision(current_arn, current_arn, previous, changed=False)
    for name, ref in images.items():
        by_name[name]["image"] = ref

    register: dict[str, Any] = {k: definition[k] for k in _REGISTER_KEYS if k in definition}
    register["containerDefinitions"] = containers
    if described.get("tags"):
        register["tags"] = described["tags"]
    new_arn = call(
        "태스크 정의 새 리비전을 등록하지 못했다",
        lambda: client.register_task_definition(**register),
    )["taskDefinition"]["taskDefinitionArn"]
    return Revision(new_arn, current_arn, previous, changed=True)


def replace_images(
    client: EcsClient,
    target: EcsService,
    images: Mapping[str, str],
    deadline: float,
    *,
    desired_count: int,
    poll_s: float = 10.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Revision:
    """images로 새 리비전을 배포하고 완료까지 기다린다. 서비스가 0개면 desired_count로 올린다."""
    revision = register_revision(client, target, images)
    stopped = _service(client, target).get("desiredCount", 0) == 0
    if not revision.changed and not stopped:
        return revision
    update: dict[str, Any] = {
        "cluster": target.cluster,
        "service": target.service,
        "taskDefinition": revision.task_definition,
    }
    if stopped:
        update["desiredCount"] = desired_count
    call("ECS 서비스를 바꾸지 못했다", lambda: client.update_service(**update))
    wait_stable(
        client, target, revision.task_definition, deadline, poll_s=poll_s, clock=clock, sleep=sleep
    )
    return Revision(
        revision.task_definition,
        revision.previous_task_definition,
        revision.previous_images,
        changed=True,
    )


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


def running_image(
    client: EcsClient, target: EcsService, container: str, task_definition: str
) -> Running:
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
        if c.get("name") == container
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
