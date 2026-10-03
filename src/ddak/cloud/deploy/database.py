"""cloud/deploy/database.py: RDS 마이그레이션(ECS 일회성 태스크). 담당 안승환(C2).

추가형 마이그레이션만. 결과는 온프렘과 같은 migration 모양(ProviderResult.migration).
AI import 금지.

내부 API(run_migration_phases)는 클러스터·서브넷·보안 그룹·태스크 정의를 인자로 받는다.
Terraform 출력 이름이 정해지면 run_migrations가 ctx에서 읽어 부른다(💭 확정 필요).

- 태스크 정의는 새 WAS 이미지를 쓰는 리비전(ecs.register_revision)이다. 서비스 교체 전에 돈다.
- 태스크 하나에서 precheck → up → verify를 차례로 돈다(`sh -c "… && … && …"`, 고정 문자열).
  Fargate 기동이 1회라 prepare_db 제한 시간(180초) 안에 들어간다. 한 단계가 실패하면(exit 1) 멈춘다.
  command override만 쓴다(WAS 이미지에 ENTRYPOINT가 없다. 앱 저장소 docker/was.Dockerfile 확인).
- 결과는 컨테이너 로그(awslogs)의 `MIGRATE_RESULT <JSON>` 줄(단계마다 한 줄, 순서대로). 로그
  그룹·스트림은 태스크 정의의 logConfiguration에서 읽는다. 다른 로그 줄은 결과·오류에 싣지 않는다.
- 결과 필드는 온프렘(onprem/deploy/migrate.py)과 같은 C-09 후보. 툴 디렉토리끼리 import할 수
  없어서 모델을 여기 따로 둔다. 바꿀 때 양쪽을 같이 고친다.
- 시간 초과면 태스크를 멈추고 ADAPTER_TIMEOUT(적용 여부 불확실).
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ddak.cloud.deploy._aws import call
from ddak.core.contracts.errors import DdakToolError, ErrorCode

PHASES = ("precheck", "up", "verify")
_COMMAND = ["sh", "-c", " && ".join(f"python -m flaskr.migrate {p} --json" for p in PHASES)]
_MIGRATION = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$"
_PREFIX = "MIGRATE_RESULT "
_MAX_LOG_PAGES = 20


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _Fingerprint(_Config):
    version: str = Field(max_length=256)
    sql_mode: str = Field(max_length=512)
    collation: str = Field(max_length=128)
    time_zone: str = Field(max_length=64)
    ssl_version: str = Field(max_length=64)


class _MigrationResult(_Config):
    phase: Literal["precheck", "up", "verify"]
    ok: bool
    current: Annotated[str, Field(pattern=_MIGRATION)] | None
    expected: Annotated[str, Field(pattern=_MIGRATION)]
    applied: list[Annotated[str, Field(pattern=_MIGRATION)]]
    signature: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fingerprint: _Fingerprint


class EcsTaskClient(Protocol):
    def describe_task_definition(self, **kwargs: Any) -> dict[str, Any]: ...

    def run_task(self, **kwargs: Any) -> dict[str, Any]: ...

    def describe_tasks(self, **kwargs: Any) -> dict[str, Any]: ...

    def stop_task(self, **kwargs: Any) -> dict[str, Any]: ...


class LogsClient(Protocol):
    def get_log_events(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class TaskNetwork:
    subnets: Sequence[str]
    security_groups: Sequence[str]
    assign_public_ip: bool = True  # 💭 퍼블릭 서브넷 + 퍼블릭 IP(Docker Hub pull)


@dataclass(frozen=True)
class MigrationTask:
    cluster: str
    task_definition: str  # 새 WAS 이미지 리비전 ARN
    container: str
    network: TaskNetwork


def run_migration_phases(
    ecs: EcsTaskClient,
    logs: LogsClient,
    task: MigrationTask,
    migrations: Sequence[str],
    deadline: float,
    *,
    poll_s: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """precheck → up → verify를 차례로 돌리고 온프렘과 같은 migration 결과를 돌려준다."""
    if any(not re.fullmatch(_MIGRATION, m) for m in migrations):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "마이그레이션 ID 오류")
    group, prefix = _log_target(ecs, task)
    task_id = _run(ecs, task)
    failure = _wait_stopped(ecs, task, task_id, deadline, poll_s=poll_s, clock=clock, sleep=sleep)
    stream = f"{prefix}/{task.container}/{task_id}"
    if failure is not None:
        done = _results_so_far(logs, group, stream)
        raise DdakToolError(
            ErrorCode.ADAPTER_FAILED, f"마이그레이션 프로세스 실패({failure}; {done})"
        )
    results = _read_results(logs, group, stream, deadline, clock=clock, sleep=sleep)
    phases: list[dict[str, Any]] = []
    for phase, result in zip(PHASES, results, strict=True):
        if result.phase != phase or not result.ok:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, f"마이그레이션 단계 실패: {phase}")
        if phase == "verify" and (
            result.current != result.expected or (migrations and result.expected != migrations[-1])
        ):
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "요청한 마이그레이션 적용 확인 실패")
        phases.append(result.model_dump())
    return {**phases[-1], "event": "MIGRATE_RESULT", "phases": phases}


def _log_target(ecs: EcsTaskClient, task: MigrationTask) -> tuple[str, str]:
    definition = call(
        "태스크 정의를 읽지 못했다",
        lambda: ecs.describe_task_definition(taskDefinition=task.task_definition),
    )["taskDefinition"]
    matched = [
        c for c in definition.get("containerDefinitions") or [] if c.get("name") == task.container
    ]
    if len(matched) != 1:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "태스크 정의에 대상 컨테이너가 없다")
    log = matched[0].get("logConfiguration") or {}
    options = log.get("options") or {}
    group, prefix = options.get("awslogs-group"), options.get("awslogs-stream-prefix")
    if log.get("logDriver") != "awslogs" or not group or not prefix:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, "마이그레이션 결과를 읽을 awslogs 설정(그룹·접두사)이 없다"
        )
    return group, prefix


def _run(ecs: EcsTaskClient, task: MigrationTask) -> str:
    response = call(
        "마이그레이션 태스크를 시작하지 못했다",
        lambda: ecs.run_task(
            cluster=task.cluster,
            taskDefinition=task.task_definition,
            launchType="FARGATE",
            count=1,
            startedBy="ddak-migrate",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": list(task.network.subnets),
                    "securityGroups": list(task.network.security_groups),
                    "assignPublicIp": "ENABLED" if task.network.assign_public_ip else "DISABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": task.container,
                        "command": list(_COMMAND),
                    }
                ]
            },
        ),
    )
    tasks = response.get("tasks") or []
    if response.get("failures") or len(tasks) != 1:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "마이그레이션 태스크를 시작하지 못했다")
    return str(tasks[0]["taskArn"]).rsplit("/", 1)[-1]


def _wait_stopped(
    ecs: EcsTaskClient,
    task: MigrationTask,
    task_id: str,
    deadline: float,
    *,
    poll_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> str | None:
    """멈출 때까지 기다린다. 정상 종료면 None, 아니면 중지 사유(식별자 가림)."""
    while True:
        tasks = (
            call(
                "마이그레이션 태스크 상태를 읽지 못했다",
                lambda: ecs.describe_tasks(cluster=task.cluster, tasks=[task_id]),
            ).get("tasks")
            or []
        )
        if len(tasks) != 1:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "마이그레이션 태스크를 찾지 못했다")
        if tasks[0].get("lastStatus") == "STOPPED":
            containers = [
                c for c in tasks[0].get("containers") or [] if c.get("name") == task.container
            ]
            if len(containers) != 1 or containers[0].get("exitCode") != 0:
                # 컨테이너가 못 떴으면(시크릿·이미지 pull 실패 등) exitCode가 없고 사유만 있다
                return _stopped_reason(tasks[0], containers)
            return None
        remaining = deadline - clock()
        if remaining <= 0:
            with contextlib.suppress(Exception):  # 멈추기 실패해도 시간 초과가 우선이다
                ecs.stop_task(cluster=task.cluster, task=task_id, reason="ddak deadline")
            raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "마이그레이션이 제한 시간을 넘겼다")
        sleep(min(poll_s, remaining))


_ARN = re.compile(r"arn:aws[a-zA-Z-]*:[^\s,;)]+")
_ACCOUNT = re.compile(r"\b\d{12}\b")


def _stopped_reason(task: dict[str, Any], containers: list[dict[str, Any]]) -> str:
    exit_code = containers[0].get("exitCode") if len(containers) == 1 else None
    reason = str(task.get("stoppedReason") or "")
    if len(containers) == 1 and containers[0].get("reason"):
        reason += " / " + str(containers[0]["reason"])
    reason = _ACCOUNT.sub("<계정>", _ARN.sub("<ARN>", reason))[:300]
    return f"exit={exit_code}, 사유={reason or '없음'}"


def _results_so_far(logs: LogsClient, group: str, stream: str) -> str:
    """실패 직후 남은 단계 결과 요약. 단계·ok·버전만 싣는다(다른 로그 줄은 싣지 않는다)."""
    try:
        lines = [m[len(_PREFIX) :] for m in _messages(logs, group, stream) if m.startswith(_PREFIX)]
    except DdakToolError:
        return "결과 로그 없음"
    parts = []
    for line in lines[: len(PHASES)]:
        try:
            r = _MigrationResult.model_validate_json(line)
        except ValidationError:
            parts.append("형식 오류")
            continue
        parts.append(f"{r.phase} ok={r.ok} current={r.current} expected={r.expected}")
    return ", ".join(parts) or "결과 줄 없음"


def _read_results(
    logs: LogsClient,
    group: str,
    stream: str,
    deadline: float,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> list[_MigrationResult]:
    """로그에서 단계별 MIGRATE_RESULT 줄을 모두 찾는다(태스크 종료 직후엔 로그가 늦을 수 있다)."""
    while True:
        lines = [
            line[len(_PREFIX) :]
            for line in _messages(logs, group, stream)
            if line.startswith(_PREFIX)
        ]
        if len(lines) > len(PHASES):
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "MIGRATE_RESULT 중복")
        if len(lines) == len(PHASES):
            try:
                return [_MigrationResult.model_validate_json(line) for line in lines]
            except ValidationError:
                raise DdakToolError(ErrorCode.ADAPTER_FAILED, "MIGRATE_RESULT 형식 오류") from None
        remaining = deadline - clock()
        if remaining <= 0:
            raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "MIGRATE_RESULT 로그를 찾지 못했다")
        sleep(min(2.0, remaining))


def _messages(logs: LogsClient, group: str, stream: str) -> list[str]:
    out: list[str] = []
    token: str | None = None
    for _ in range(_MAX_LOG_PAGES):
        kwargs: dict[str, Any] = {
            "logGroupName": group,
            "logStreamName": stream,
            "startFromHead": True,
        }
        if token:
            kwargs["nextToken"] = token
        try:
            page = logs.get_log_events(**kwargs)
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code == "ResourceNotFoundException":  # 스트림이 아직 안 생김
                return out
            raise DdakToolError(
                ErrorCode.ADAPTER_FAILED, "마이그레이션 로그를 읽지 못했다"
            ) from exc
        out += [str(e.get("message", "")).rstrip("\n") for e in page.get("events") or []]
        following = page.get("nextForwardToken")
        if not following or following == token:
            return out
        token = following
    return out
