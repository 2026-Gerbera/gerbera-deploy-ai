"""cloud/deploy/ecs.py: ECS Fargate 배포·롤백. 담당 안승환(C2).

태스크 정의 새 리비전(이미지 = RunContext.images의 digest 고정 참조, secrets valueFrom = 인프라
출력의 시크릿 ARN) -> update_service. boto3는 호출마다 새 Session. AI import 금지.

- 배치(R9 (a), 10/2 C2 결정): 서비스 1개, 태스크 하나에 web·was 컨테이너. 그래서 이미지는
  {컨테이너: 참조}로 받아 바뀐 컨테이너를 한 리비전에 모아 한 번만 전환한다.
- 배포 방식은 Terraform이 서비스 deploymentConfiguration.strategy로 정한다. 10/3 결정(정준우):
  클라우드는 ECS 네이티브 블루그린(CodeDeploy 아님, 100% 한 번에 전환, bake 1분). 호출은 같다
  (새 리비전 → update_service → 완료 대기). 완료 판정만 다르다.
  - BLUE_GREEN: update_service 응답의 currentServiceDeployment를 describe_service_deployments로
    2초마다 본다. lifecycleStage가 BAKE_TIME·CLEAN_UP이거나 status SUCCESSFUL이면 성공(운영
    트래픽 전환 완료), STOPPED·STOP_REQUESTED·ROLLBACK_*이면 실패. 시작 전에 직전 배포가 롤백
    중이면 끝날 때까지 기다린다.
  - 롤링(그 밖): PRIMARY가 COMPLETED이고 이전 배포가 모두 빠지면 성공, FAILED면 실패.
- 새 리비전은 현재 서비스의 태스크 정의를 그대로 복사하고 지정한 컨테이너의 image를 바꾼다.
  Docker Hub repositoryCredentials·역할은 Terraform이 만든 값을 유지한다. Terraform은 컨테이너
  environment·secrets를 두지 않으므로(generate_infra 프롬프트) 런타임 설정은 이 층이
  {컨테이너: {이름: 값}}으로 넣고, 넣은 컨테이너의 목록은 통째로 그 값으로 맞춘다.
- 이미지·환경·시크릿이 모두 현재 리비전과 같으면 새 리비전을 만들지 않는다(tier step마다
  부르지만 서비스 전환은 1회).
- Terraform은 서비스를 태스크 0개로 만든다. 첫 배포 때 desired_count로 올린다.
- 이미지는 digest 고정 참조만 받는다. 태그로 배포하지 않는다.
- 롤백: 진행 중(PENDING·IN_PROGRESS) 배포가 있으면 stop_service_deployment(ROLLBACK)으로 먼저
  되돌리고(rollback_in_flight), 그 뒤 이전 릴리스 이미지로 새 리비전(이미 같으면 그대로).
  회로 차단기가 자동 롤백 중이면 끝나기를 기다릴 뿐 다시 롤백하지 않는다.
  이전 리비전 ARN에 기대지 않는다.
- 폴링 간격은 2초(POLL_S).
- 오류 메시지에 ARN·계정 ID·AWS 오류 원문을 넣지 않는다.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ddak.cloud.deploy._aws import call
from ddak.core.contracts.errors import DdakToolError, ErrorCode

POLL_S = 2.0
_BG_DONE_STAGES = frozenset({"BAKE_TIME", "CLEAN_UP"})
_ROLLBACK_ACTIVE = ("ROLLBACK_REQUESTED", "ROLLBACK_IN_PROGRESS")
_BG_FAILED = frozenset(
    {"STOPPED", "STOP_REQUESTED", *_ROLLBACK_ACTIVE, "ROLLBACK_SUCCESSFUL", "ROLLBACK_FAILED"}
)
_ACTIVE = ("PENDING", "IN_PROGRESS")
_IMAGE_REF = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
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

    def list_service_deployments(self, **kwargs: Any) -> dict[str, Any]: ...

    def describe_service_deployments(self, **kwargs: Any) -> dict[str, Any]: ...

    def stop_service_deployment(self, **kwargs: Any) -> dict[str, Any]: ...


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


def register_revision(
    client: EcsClient,
    target: EcsService,
    images: Mapping[str, str],
    *,
    environment: Mapping[str, Mapping[str, str]] | None = None,
    secrets: Mapping[str, Mapping[str, str]] | None = None,
    only_containers: frozenset[str] | None = None,
) -> Revision:
    """서비스의 현재 태스크 정의를 복사해 images({컨테이너: 참조})와 런타임 설정을 바꾼 새 리비전을
    등록한다. environment·secrets는 {컨테이너: {이름: 값}}이고 secrets 값은 시크릿 ARN이다.

    서비스는 바꾸지 않는다. 마이그레이션(database.py)이 서비스 교체 전에 이 리비전으로 돈다
    (only_containers로 was만 남긴다).
    """
    if not images:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "바꿀 컨테이너 이미지가 없다")
    if any(not _IMAGE_REF.fullmatch(ref) for ref in images.values()):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "이미지는 digest 고정 참조여야 한다")
    env = _checked_settings(environment)
    sec = _checked_settings(secrets)
    current_arn = _service(client, target)["taskDefinition"]
    described = call(
        "태스크 정의를 읽지 못했다",
        lambda: client.describe_task_definition(taskDefinition=current_arn, include=["TAGS"]),
    )
    definition = described["taskDefinition"]
    containers = [dict(c) for c in definition.get("containerDefinitions") or []]
    by_name = {c.get("name"): c for c in containers}
    missing = sorted((set(images) | env.keys() | sec.keys()) - by_name.keys())
    if missing:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, f"태스크 정의에 컨테이너가 없다: {', '.join(missing)}"
        )
    if only_containers is not None and not only_containers:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "실행할 컨테이너가 없다")
    if only_containers is not None and not only_containers <= by_name.keys():
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "태스크 정의에 실행할 컨테이너가 없다")
    previous = {name: by_name[name].get("image") for name in images}
    same = (
        all(previous[name] == ref for name, ref in images.items())
        and all(_pairs(by_name[c].get("environment"), "value") == v for c, v in env.items())
        and all(_pairs(by_name[c].get("secrets"), "valueFrom") == v for c, v in sec.items())
    )
    if only_containers is None and same:
        return Revision(current_arn, current_arn, previous, changed=False)
    for name, ref in images.items():
        by_name[name]["image"] = ref
    for name, values in env.items():
        by_name[name]["environment"] = [{"name": k, "value": v} for k, v in sorted(values.items())]
    for name, values in sec.items():
        by_name[name]["secrets"] = [{"name": k, "valueFrom": v} for k, v in sorted(values.items())]
    if only_containers is not None:
        containers = [c for c in containers if c.get("name") in only_containers]
        for container in containers:
            container["essential"] = True

    register: dict[str, Any] = {k: definition[k] for k in _REGISTER_KEYS if k in definition}
    register["containerDefinitions"] = containers
    if described.get("tags"):
        register["tags"] = described["tags"]
    new_arn = call(
        "태스크 정의 새 리비전을 등록하지 못했다",
        lambda: client.register_task_definition(**register),
    )["taskDefinition"]["taskDefinitionArn"]
    return Revision(new_arn, current_arn, previous, changed=True)


def _checked_settings(
    settings: Mapping[str, Mapping[str, str]] | None,
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for container, values in (settings or {}).items():
        if any(
            not _ENV_NAME.fullmatch(k)
            or not isinstance(v, str)
            or not v
            or any(c in v for c in "\r\n\0")
            for k, v in values.items()
        ):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "컨테이너 환경 변수 형식 오류")
        result[container] = dict(values)
    return result


def _pairs(items: Any, value_key: str) -> dict[str, Any]:
    return {i.get("name"): i.get(value_key) for i in items or []}


def replace_images(
    client: EcsClient,
    target: EcsService,
    images: Mapping[str, str],
    deadline: float,
    *,
    desired_count: int,
    environment: Mapping[str, Mapping[str, str]] | None = None,
    secrets: Mapping[str, Mapping[str, str]] | None = None,
    poll_s: float = POLL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Revision:
    """images로 새 리비전을 배포하고 완료까지 기다린다. 서비스가 0개면 desired_count로 올린다."""
    revision = register_revision(client, target, images, environment=environment, secrets=secrets)
    service = _service(client, target)
    stopped = service.get("desiredCount", 0) == 0
    if not revision.changed and not stopped:
        return revision
    blue_green = _blue_green(service)
    if blue_green:
        _wait_no_rollback(client, target, deadline, poll_s=poll_s, clock=clock, sleep=sleep)
    update: dict[str, Any] = {
        "cluster": target.cluster,
        "service": target.service,
        "taskDefinition": revision.task_definition,
    }
    if stopped:
        update["desiredCount"] = desired_count
    updated = call("ECS 서비스를 바꾸지 못했다", lambda: client.update_service(**update))
    deployment = (updated.get("service") or {}).get("currentServiceDeployment")
    if blue_green and isinstance(deployment, str) and deployment:
        wait_service_deployment(
            client, deployment, deadline, poll_s=poll_s, clock=clock, sleep=sleep
        )
    else:
        wait_stable(
            client,
            target,
            revision.task_definition,
            deadline,
            poll_s=poll_s,
            clock=clock,
            sleep=sleep,
        )
    return Revision(
        revision.task_definition,
        revision.previous_task_definition,
        revision.previous_images,
        changed=True,
    )


def wait_service_deployment(
    client: EcsClient,
    deployment_arn: str,
    deadline: float,
    *,
    poll_s: float = POLL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """블루그린 배포가 운영 트래픽 전환을 마칠 때까지 본다(BAKE_TIME 진입이 완료 시점)."""
    while True:
        current = _service_deployment(client, deployment_arn)
        status, stage = current.get("status"), current.get("lifecycleStage")
        if status == "SUCCESSFUL" or (status not in _BG_FAILED and stage in _BG_DONE_STAGES):
            return
        if status in _BG_FAILED:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, f"ECS 블루그린 배포 실패({status})")
        remaining = deadline - clock()
        if remaining <= 0:
            raise DdakToolError(
                ErrorCode.ADAPTER_TIMEOUT, "ECS 배포가 제한 시간 안에 끝나지 않았다"
            )
        sleep(min(poll_s, remaining))


def rollback_in_flight(
    client: EcsClient,
    target: EcsService,
    deadline: float,
    *,
    poll_s: float = POLL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """진행 중 배포를 ECS 롤백으로 되돌리거나, 이미 롤백 중이면 끝나기를 기다린다.

    한 일이 있으면 결과에 남길 문장, 없으면 None. 자동 롤백 중인 배포를 다시 롤백하지 않는다.
    """
    active = _deployments(client, target, _ACTIVE)
    rolling_back = _deployments(client, target, _ROLLBACK_ACTIVE)
    if active:
        arn = active[0]["serviceDeploymentArn"]
        call(
            "ECS 배포 롤백을 요청하지 못했다",
            lambda: client.stop_service_deployment(serviceDeploymentArn=arn, stopType="ROLLBACK"),
        )
        _wait_rollback_done(client, arn, deadline, poll_s=poll_s, clock=clock, sleep=sleep)
        return "진행 중 배포를 ECS 롤백으로 되돌렸다"
    if rolling_back:
        arn = rolling_back[0]["serviceDeploymentArn"]
        _wait_rollback_done(client, arn, deadline, poll_s=poll_s, clock=clock, sleep=sleep)
        return "회로 차단기가 이미 자동 롤백했다(다시 롤백하지 않음)"
    return None


def _wait_rollback_done(
    client: EcsClient,
    deployment_arn: str,
    deadline: float,
    *,
    poll_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    while True:
        status = _service_deployment(client, deployment_arn).get("status")
        if status in ("ROLLBACK_SUCCESSFUL", "STOPPED", "SUCCESSFUL"):
            return
        if status == "ROLLBACK_FAILED":
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "ECS 롤백이 실패했다")
        remaining = deadline - clock()
        if remaining <= 0:
            raise DdakToolError(
                ErrorCode.ADAPTER_TIMEOUT, "ECS 롤백이 제한 시간 안에 끝나지 않았다"
            )
        sleep(min(poll_s, remaining))


def _wait_no_rollback(
    client: EcsClient,
    target: EcsService,
    deadline: float,
    *,
    poll_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    """직전 배포가 롤백 중이면 끝날 때까지 기다린다(그 위에 새 배포를 얹지 않는다)."""
    while _deployments(client, target, _ROLLBACK_ACTIVE):
        remaining = deadline - clock()
        if remaining <= 0:
            raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "직전 ECS 롤백이 끝나지 않았다")
        sleep(min(poll_s, remaining))


def _blue_green(service: Mapping[str, Any]) -> bool:
    return (service.get("deploymentConfiguration") or {}).get("strategy") == "BLUE_GREEN"


def _deployments(
    client: EcsClient, target: EcsService, statuses: Sequence[str]
) -> list[dict[str, Any]]:
    return list(
        call(
            "ECS 배포 목록을 읽지 못했다",
            lambda: client.list_service_deployments(
                cluster=target.cluster, service=target.service, status=list(statuses)
            ),
        ).get("serviceDeployments")
        or []
    )


def _service_deployment(client: EcsClient, deployment_arn: str) -> Mapping[str, Any]:
    found = (
        call(
            "ECS 배포 상태를 읽지 못했다",
            lambda: client.describe_service_deployments(serviceDeploymentArns=[deployment_arn]),
        ).get("serviceDeployments")
        or []
    )
    if len(found) != 1:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "ECS 배포를 찾지 못했다")
    return found[0]


def wait_stable(
    client: EcsClient,
    target: EcsService,
    task_definition: str,
    deadline: float,
    *,
    poll_s: float = POLL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """롤링 배포가 끝날 때까지 읽기 폴링한다."""
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
    poll_s: float = POLL_S,
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
