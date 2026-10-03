"""승인된 온프렘 준비의 제품 adapter. 기본값은 실제 runner, 테스트는 fake만 주입한다."""

from __future__ import annotations

import fcntl
import os
import re
import stat
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import ErrorCode
from ddak.onprem.deploy.containers import DockerHost, Runner, fail, subprocess_runner
from ddak.onprem.deploy.preparation import (
    ApprovalCheck,
    ContainerObservation,
    DatabaseAccount,
    DatabaseObservation,
    DatabasePreparationPlan,
    DatabasePreparationResult,
    OwnerTransferPlan,
    TableObservation,
    _identifier,
    _sha,
    apply_db_preparation,
    apply_owner_transfer,
    plan_db_preparation,
    plan_owner_transfer,
)
from ddak.onprem.deploy.provider import OnPremProvider, _Tier
from ddak.onprem.deploy.registration import _directory
from ddak.onprem.deploy.replicas import names


class StdinRunner(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        input: str,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


def _stdin_runner(
    argv: list[str],
    *,
    input: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, input=input, timeout=timeout, text=True, capture_output=True, check=False
    )


class _RoutedRunner:
    """SSH session이 붙인 argv prefix를 유지하면서 단일 호출에만 stdin을 연결한다."""

    def __init__(self, runner: Runner, stdin_runner: StdinRunner):
        self.runner = runner
        self.stdin_runner = stdin_runner
        self.payload: str | None = None

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        if self.payload is not None:
            return self.stdin_runner(argv, input=self.payload, timeout=timeout)
        return self.runner(argv, timeout=timeout)


_MYSQL = (
    'test -n "$MYSQL_ROOT_PASSWORD" || exit 1; '
    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --user=root '
    "--batch --raw --skip-column-names --skip-line-numbers"
)


class MySQLPreparationSession:
    """기존 owned MySQL만 준비한다. SQL·비밀번호를 로그/결과에 반환하지 않는다."""

    def __init__(
        self,
        host: DockerHost,
        config: _Tier,
        ctx: RunContext,
        routed: _RoutedRunner,
        *,
        password_reader: Callable[[str], str | None] | None = None,
    ):
        self.host, self.config, self.ctx, self.routed = host, config, ctx, routed
        self.password_reader = password_reader
        self.bound_identity: str | None = None

    def _container(self) -> dict[str, Any]:
        state = self.host.container(self.config.name)
        if not state:
            raise fail("기존 DB 컨테이너 필요; 생성/재생성 금지", ErrorCode.PRECONDITION_FAILED)
        self.host.owned(state, self.ctx.project, "db")
        volume = self.config.volumes[0]
        if (
            self.config.kind != "mysql"
            or not state.get("State", {}).get("Running")
            or len(state.get("Mounts", [])) != 1
            or not any(
                m.get("Type") == "volume"
                and m.get("Name") == volume.name
                and m.get("Destination") == "/var/lib/mysql"
                and m.get("RW") is True
                for m in state.get("Mounts", [])
            )
            or (
                self.ctx.images.get("db")
                and state.get("Config", {}).get("Image") != self.ctx.images["db"]
            )
        ):
            raise fail("DB 실행/이미지/영구볼륨 불일치; 상태 보존", ErrorCode.PRECONDITION_FAILED)
        identity = _sha(
            {
                "id": state["Id"],
                "image": state["Image"],
                "mounts": state["Mounts"],
                "labels": state["Config"].get("Labels"),
            }
        )
        if self.bound_identity is not None and self.bound_identity != identity:
            raise fail("DB session 대상 변경; 실행 중단", ErrorCode.PRECONDITION_FAILED)
        self.bound_identity = identity
        return state

    def _query(self, sql: str, *, secret_stdin: bool = False) -> str:
        state = self._container()
        try:
            if secret_stdin:
                self.routed.payload = sql
                result = self.host.run("container", "exec", "-i", state["Id"], "sh", "-c", _MYSQL)
            else:
                result = self.host.run(
                    "container",
                    "exec",
                    state["Id"],
                    "sh",
                    "-c",
                    _MYSQL + ' --execute "$1"',
                    "ddak-mysql",
                    sql,
                )
            return result.stdout
        except Exception:
            raise fail("DB 준비 명령 실패; 출력 비공개", ErrorCode.PRECONDITION_FAILED) from None
        finally:
            self.routed.payload = None

    def observe(
        self, database: str, backup_database: str, accounts: tuple[str, ...]
    ) -> DatabaseObservation:
        for value in (database, backup_database, *accounts):
            _identifier(value)
        dbs = f"'{database}','{backup_database}'"
        users = ",".join(f"'{name}'" for name in accounts)
        grantees = ",".join(
            f"CONCAT(CHAR(39),'{name}',CHAR(39),'@',CHAR(39),'%',CHAR(39))" for name in accounts
        )
        sql = (
            "SELECT 'database',SCHEMA_NAME FROM information_schema.SCHEMATA "  # noqa: S608
            f"WHERE SCHEMA_NAME IN ({dbs}); "
            "SELECT 'table',t.TABLE_SCHEMA,t.TABLE_NAME, "
            "IF(t.TABLE_TYPE='BASE TABLE' AND t.ENGINE='InnoDB' AND NOT EXISTS "
            "(SELECT 1 FROM information_schema.TRIGGERS r WHERE "
            "r.EVENT_OBJECT_SCHEMA=t.TABLE_SCHEMA AND r.EVENT_OBJECT_TABLE=t.TABLE_NAME) "
            "AND NOT EXISTS (SELECT 1 FROM information_schema.KEY_COLUMN_USAGE k WHERE "
            "k.REFERENCED_TABLE_NAME IS NOT NULL AND ((k.TABLE_SCHEMA=t.TABLE_SCHEMA "
            "AND k.TABLE_NAME=t.TABLE_NAME) OR (k.REFERENCED_TABLE_SCHEMA=t.TABLE_SCHEMA "
            "AND k.REFERENCED_TABLE_NAME=t.TABLE_NAME))),1,0) "
            f"FROM information_schema.TABLES t WHERE t.TABLE_SCHEMA IN ({dbs}); "
            f"SELECT 'account',User FROM mysql.user WHERE Host='%' AND User IN ({users}); "
            "SELECT 'grant',GRANTEE,TABLE_SCHEMA,PRIVILEGE_TYPE "
            f"FROM information_schema.SCHEMA_PRIVILEGES WHERE TABLE_SCHEMA IN ({dbs}) "
            f"AND GRANTEE IN ({grantees});"
        )
        state = self._container()
        daemon = self.host.run("info", "--format", "{{.ID}}").stdout.strip()
        if not daemon:
            raise fail("Docker daemon ID 관측 실패", ErrorCode.PRECONDITION_FAILED)
        target = _sha(
            {
                "daemon": daemon,
                "container": state["Id"],
                "image": state["Image"],
                "mounts": state["Mounts"],
                "inventory": self.config.model_dump(mode="json"),
            }
        )
        databases, found_accounts, tables = [], [], []
        grants: dict[tuple[str, str], list[str]] = {}
        try:
            for line in self._query(sql).splitlines():
                parts = line.split("\t")
                if parts[0] == "database" and len(parts) == 2:
                    databases.append(parts[1])
                elif parts[0] == "table" and len(parts) == 4 and parts[3] in {"0", "1"}:
                    tables.append(TableObservation(parts[1], parts[2], parts[3] == "1"))
                elif parts[0] == "account" and len(parts) == 2:
                    found_accounts.append(parts[1])
                elif parts[0] == "grant" and len(parts) == 4:
                    match = re.fullmatch(r"'([A-Za-z_][A-Za-z0-9_]*)'@'%'", parts[1])
                    if not match:
                        raise ValueError("grantee")
                    grants.setdefault((match[1], parts[2]), []).append(parts[3])
                else:
                    raise ValueError("row")
            return DatabaseObservation(
                target,
                tuple(databases),
                tuple(tables),
                tuple(found_accounts),
                tuple((name, db, tuple(privileges)) for (name, db), privileges in grants.items()),
            )
        except Exception:
            raise fail("MySQL 테이블/계정 관측 형식 오류", ErrorCode.PRECONDITION_FAILED) from None

    def execute(self, sql: str) -> None:
        # 공개 adapter에서도 임의 SQL은 받지 않는다. helper가 생성하는 문법만 허용한다.
        identifier = r"[A-Za-z_][A-Za-z0-9_]{0,63}"
        create = rf"CREATE DATABASE `{identifier}`"
        pair = rf"`{identifier}`\.`{identifier}` TO `{identifier}`\.`{identifier}`"
        rename = rf"RENAME TABLE {pair}(?:, {pair})*"
        grant = (
            rf"GRANT (?:ALTER|CREATE|DELETE|INDEX|INSERT|REFERENCES|SELECT|UPDATE)"
            rf"(?:, (?:ALTER|CREATE|DELETE|INDEX|INSERT|REFERENCES|SELECT|UPDATE))* "
            rf"ON `{identifier}`\.\* TO '{identifier}'@'%'"
        )
        if not any(re.fullmatch(pattern, sql) for pattern in (create, rename, grant)):
            raise fail("임의 DB 준비 SQL 금지", ErrorCode.CONFIG_INVALID)
        self.host.mutation_started = True
        self._query(sql)

    def create_account(self, name: str, *, password_env_ref: str) -> None:
        _identifier(name)
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", password_env_ref):
            raise fail("DB 비밀번호 환경 참조 오류", ErrorCode.CONFIG_INVALID)
        # plan/observe에서는 이 경로를 타지 않는다. 실제 승인 후 코드 실행에서만 읽는다.
        try:
            value = (
                self.password_reader(password_env_ref)
                if self.password_reader is not None
                else os.environ.get(password_env_ref)
            )
        except Exception:
            raise fail("DB 비밀번호 참조 조회 실패", ErrorCode.CONFIG_INVALID) from None
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 4096
            or any(c in value for c in "\0\n\r")
        ):
            raise fail("DB 비밀번호 환경 참조 공급 필요", ErrorCode.CONFIG_INVALID)
        # CREATE USER 인증 문자열의 escaping이 세션 SQL mode에 영향받지 않게 고정한다.
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        sql = f"SET SESSION sql_mode='';\nCREATE USER '{name}'@'%' IDENTIFIED BY '{escaped}';\n"
        self.host.mutation_started = True
        self._query(sql, secret_stdin=True)


_TRANSFER_FORMAT = (
    '{"Id":{{json .Id}},"Image":{{json .Image}},"Name":{{json .Name}},'
    '"Config":{"Labels":{{json .Config.Labels}},"Cmd":{{json .Config.Cmd}},'
    '"Entrypoint":{{json .Config.Entrypoint}},"User":{{json .Config.User}},'
    '"WorkingDir":{{json .Config.WorkingDir}},"Healthcheck":{{json .Config.Healthcheck}},'
    '"StopSignal":{{json .Config.StopSignal}},"StopTimeout":{{json .Config.StopTimeout}},'
    '"Tty":{{json .Config.Tty}},"OpenStdin":{{json .Config.OpenStdin}},'
    '"StdinOnce":{{json .Config.StdinOnce}},"ExposedPorts":{{json .Config.ExposedPorts}},'
    '"Volumes":{{json .Config.Volumes}}},'
    '"HostConfig":{{json .HostConfig}},'
    '"Networks":{{json .NetworkSettings.Networks}},'
    '"Mounts":{{json .Mounts}},"State":{"Running":{{json .State.Running}}}}'
)


class DockerOwnershipTransfer:
    """WEB/WAS만 복제한다. 원래 컨테이너는 stopped backup으로 남기고 볼륨을 보존한다."""

    def __init__(self, host: DockerHost, config: _Tier, ctx: RunContext, name: str):
        self.host, self.config, self.ctx, self.name = host, config, ctx, name

    def _state(self) -> dict[str, Any]:
        found = self.host.container(self.name)
        if not found:
            raise fail("이전할 컨테이너 없음", ErrorCode.PRECONDITION_FAILED)
        result = self.host.json("container", "inspect", "--format", _TRANSFER_FORMAT, found["Id"])
        if not isinstance(result, dict) or result.get("Id") != found["Id"]:
            raise fail("소유권 이전 inspect 불일치", ErrorCode.PRECONDITION_FAILED)
        return result

    def observe(self) -> ContainerObservation:
        state = self._state()
        host_config, config = state["HostConfig"], state["Config"]
        if (
            any(
                host_config.get(key)
                for key in (
                    "Privileged",
                    "Binds",
                    "CapAdd",
                    "Devices",
                    "SecurityOpt",
                    "ReadonlyRootfs",
                )
            )
            or host_config["NetworkMode"] != self.config.network
            or set(state.get("Networks", {})) != {self.config.network}
            or any(m.get("Type") != "volume" for m in state["Mounts"])
            or len(config.get("Entrypoint") or []) > 1
        ):
            raise fail(
                "현재 이전 adapter가 보존할 수 없는 실행 설정", ErrorCode.PRECONDITION_FAILED
            )
        volumes = tuple(sorted((m["Name"], m["Destination"], m["RW"]) for m in state["Mounts"]))
        if volumes != tuple(
            sorted((v.name, v.target, not v.read_only) for v in self.config.volumes)
        ):
            raise fail("승인 인벤토리와 기존 볼륨 불일치", ErrorCode.PRECONDITION_FAILED)
        daemon = self.host.run("info", "--format", "{{.ID}}").stdout.strip()
        if not daemon:
            raise fail("Docker daemon ID 관측 실패", ErrorCode.PRECONDITION_FAILED)
        # Env 필드는 아예 조회하지 않는다. env는 main의 승인된 env_file을 사용한다.
        stable = {
            "Config": {k: v for k, v in config.items() if k != "Labels"},
            "HostConfig": host_config,
            "inventory": self.config.model_dump(mode="json"),
        }
        return ContainerObservation(
            daemon,
            state["Id"],
            state["Name"].removeprefix("/"),
            state["Image"],
            _sha(stable),
            tuple(sorted((config.get("Labels") or {}).items())),
            volumes,
            state["State"]["Running"],
        )

    def transfer(self, plan: OwnerTransferPlan) -> None:
        if plan.tier not in {"web", "was"} or self.config.kind == "mysql":
            raise fail("DB 컨테이너 이전 금지", ErrorCode.PRECONDITION_FAILED)
        if self.observe() != plan.observed:
            raise fail("이전 직전 상태 변경", ErrorCode.PRECONDITION_FAILED)
        state = self._state()
        old = plan.observed.container_id
        suffix = plan.approval_sha[:16]
        temporary, backup = (
            self.name + "-ddak-adopt-" + suffix,
            self.name + "-ddak-before-" + suffix,
        )
        if self.host.container(temporary) or self.host.container(backup):
            raise fail("소유권 이전 잔여 컨테이너; 수동 확인 필요", ErrorCode.PRECONDITION_FAILED)
        config, host_config = state["Config"], state["HostConfig"]
        args = OnPremProvider._args(self.config, self.ctx.project, plan.tier)
        # 이전 label은 target owner 키를 제외하고 보존한다.
        for key, value in (config.get("Labels") or {}).items():
            if key not in plan.owner_labels:
                args += ["--label", f"{key}={value}"]
        for port, bindings in (host_config.get("PortBindings") or {}).items():
            if not re.fullmatch(r"[0-9]{1,5}/tcp", port):
                raise fail("지원하지 않는 publish 포트", ErrorCode.PRECONDITION_FAILED)
            for binding in bindings:
                value = f"{binding['HostIp']}:{binding['HostPort']}:{port}"
                _Tier.valid_ports([value])
                args += ["--publish", value]
        for field_name, flag in (("User", "--user"), ("WorkingDir", "--workdir")):
            if config.get(field_name):
                args += [flag, config[field_name]]
        if config.get("StopSignal"):
            args += ["--stop-signal", config["StopSignal"]]
        if config.get("StopTimeout") is not None:
            args += ["--stop-timeout", str(config["StopTimeout"])]
        if config.get("Tty") or config.get("OpenStdin") or config.get("StdinOnce"):
            raise fail("대화형 컨테이너는 자동 이전 미지원", ErrorCode.PRECONDITION_FAILED)
        args += ["--entrypoint", (config.get("Entrypoint") or [""])[0]]
        health = config.get("Healthcheck") or {}
        test = health.get("Test") or []
        if test:
            if test == ["NONE"]:
                args += ["--no-healthcheck"]
            elif len(test) == 2 and test[0] == "CMD-SHELL":
                args += ["--health-cmd", test[1]]
                for key, flag in (
                    ("Interval", "--health-interval"),
                    ("Timeout", "--health-timeout"),
                    ("StartPeriod", "--health-start-period"),
                    ("StartInterval", "--health-start-interval"),
                ):
                    if health.get(key):
                        args += [flag, str(health[key]) + "ns"]
                if health.get("Retries"):
                    args += ["--health-retries", str(health["Retries"])]
            else:
                raise fail("지원하지 않는 healthcheck 이전", ErrorCode.PRECONDITION_FAILED)
        policy = host_config.get("RestartPolicy") or {}
        restart = policy.get("Name") or "no"
        if restart == "on-failure" and policy.get("MaximumRetryCount"):
            restart += ":" + str(policy["MaximumRetryCount"])
        args += ["--restart", restart]
        aliases = state["Networks"][self.config.network].get("Aliases") or []
        for alias in sorted(set(aliases) | {self.name}):
            if alias not in {old, old[:12]}:
                args += ["--network-alias", alias]
        # 모든 볼륨은 이미 존재해야 한다. create/삭제/driver 변경을 수행하지 않는다.
        for volume in self.config.volumes:
            meta = self.host.json(
                "volume",
                "inspect",
                "--format",
                '{"Driver":{{json .Driver}},"Options":{{json .Options}}}',
                volume.name,
            )
            if meta.get("Driver") != "local" or meta.get("Options"):
                raise fail("이전 볼륨 driver 불일치", ErrorCode.PRECONDITION_FAILED)
        self.host.run(
            "container",
            "create",
            "--name",
            temporary,
            *args,
            state["Image"],
            *(config.get("Cmd") or []),
        )
        created = self.host.container(temporary)
        if not created:
            raise fail("새 이전 컨테이너 생성 미확인", ErrorCode.PRECONDITION_FAILED)
        self.host.owned(created, self.ctx.project, plan.tier)
        candidate = DockerOwnershipTransfer(self.host, self.config, self.ctx, temporary).observe()
        expected = replace(
            plan.observed,
            container_id=candidate.container_id,
            name=temporary,
            labels=tuple(sorted({**dict(plan.observed.labels), **plan.owner_labels}.items())),
            running=False,
        )
        if candidate != expected:
            raise fail(
                "새 이전 컨테이너 실행 설정 불일치; 원본 보존", ErrorCode.PRECONDITION_FAILED
            )
        if self.observe() != plan.observed:
            raise fail("이전 도중 원래 컨테이너 변경; 원본 보존", ErrorCode.PRECONDITION_FAILED)
        if plan.observed.running:
            self.host.run("container", "stop", "--time", "10", old)
        if self.observe() != replace(plan.observed, running=False):
            raise fail("중지 뒤 원본 관측 변경; 자동 이전 중단", ErrorCode.PRECONDITION_FAILED)
        self.host.run("container", "rename", old, backup)
        self.host.run("container", "rename", created["Id"], self.name)
        if plan.observed.running:
            self.host.run("container", "start", created["Id"])
        # helper가 새 ID·owner·실행 설정·볼륨을 최종 재관측한다. 실패 시 자동 삭제/claim 없음.


class OnPremPreparationManager:
    """setup UI에 주입할 plan/apply 객체. 승인 저장소/계약 모델을 직접 변경하지 않는다."""

    def __init__(
        self,
        root: Path,
        ctx: RunContext,
        *,
        runner: Runner = subprocess_runner,
        stdin_runner: StdinRunner = _stdin_runner,
        password_reader: Callable[[str], str | None] | None = None,
    ):
        self.root, self.ctx = root.absolute(), ctx
        self.routed = _RoutedRunner(runner, stdin_runner)
        self.provider = OnPremProvider(self.routed)
        self.password_reader = password_reader

    @contextmanager
    def _lease(self) -> Iterator[None]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.ctx.project):
            raise fail("준비 프로젝트 식별자 오류", ErrorCode.CONFIG_INVALID)
        directory = _directory(self.root / "settings" / self.ctx.project)
        try:
            fd = os.open(
                "preparation.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(fd, "a") as stream:
                meta = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(meta.st_mode)
                    or meta.st_uid != os.getuid()
                    or meta.st_nlink != 1
                ):
                    raise fail("준비 lock 파일 오류", ErrorCode.PRECONDITION_FAILED)
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise fail(
                        "동일 프로젝트 준비 실행 중", ErrorCode.PRECONDITION_FAILED
                    ) from None
                yield
        except OSError:
            raise fail("준비 lock 접근 실패", ErrorCode.PRECONDITION_FAILED) from None
        finally:
            os.close(directory)

    @contextmanager
    def _database(self) -> Iterator[MySQLPreparationSession]:
        with self.provider._session("db", self.ctx) as (host, config):
            yield MySQLPreparationSession(
                host, config, self.ctx, self.routed, password_reader=self.password_reader
            )

    def plan_database(
        self, *, database: str, backup_database: str, accounts: tuple[DatabaseAccount, ...]
    ) -> DatabasePreparationPlan:
        if self.ctx.mode.value != "bootstrap":
            raise fail("DB 준비는 BOOTSTRAP만 허용", ErrorCode.PRECONDITION_FAILED)
        with self._lease(), self._database() as session:
            return plan_db_preparation(
                project=self.ctx.project,
                database=database,
                backup_database=backup_database,
                accounts=accounts,
                session=session,
                mode=self.ctx.mode.value,
            )

    def apply_database(
        self, plan: DatabasePreparationPlan, *, approved_sha: str, approval_check: ApprovalCheck
    ) -> DatabasePreparationResult:
        if self.ctx.mode.value != "bootstrap" or plan.project != self.ctx.project:
            raise fail("다른 모드/프로젝트 DB 준비 금지", ErrorCode.PRECONDITION_FAILED)
        with self._lease(), self._database() as session:
            return apply_db_preparation(
                plan, approved_sha=approved_sha, approval_check=approval_check, session=session
            )

    @contextmanager
    def _ownership(self, tier: str, replica: int) -> Iterator[DockerOwnershipTransfer]:
        if tier not in {"web", "was", "db"}:
            raise fail("소유권 이전 tier 오류", ErrorCode.CONFIG_INVALID)
        with self.provider._session(tier, self.ctx) as (host, config):
            candidates = names(config)
            if type(replica) is not int or not 1 <= replica <= len(candidates):
                raise fail("소유권 이전 replica 오류", ErrorCode.CONFIG_INVALID)
            yield DockerOwnershipTransfer(host, config, self.ctx, candidates[replica - 1][1])

    def plan_ownership(self, tier: str, *, replica: int = 1) -> OwnerTransferPlan:
        with self._lease(), self._ownership(tier, replica) as adapter:
            return plan_owner_transfer(project=self.ctx.project, tier=tier, observe=adapter.observe)

    def apply_ownership(
        self,
        plan: OwnerTransferPlan,
        *,
        approved_sha: str,
        approval_check: ApprovalCheck,
        replica: int = 1,
    ) -> ContainerObservation:
        if plan.project != self.ctx.project:
            raise fail("다른 프로젝트 소유권 이전 금지", ErrorCode.PRECONDITION_FAILED)
        with self._lease(), self._ownership(plan.tier, replica) as adapter:
            return apply_owner_transfer(
                plan,
                approved_sha=approved_sha,
                approval_check=approval_check,
                observe=adapter.observe,
                transfer=adapter.transfer,
            )
