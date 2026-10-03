"""B6/B7 제안과 승인 후 코드 실행. 관측·실행은 main이 주입하며 기본 실행기는 없다."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Literal, Protocol

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.onprem.deploy.containers import fail

ApprovalCheck = Callable[[str, str], bool]
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_ENV_REFERENCE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}\Z")
_RESERVED = {"mysql", "sys", "information_schema", "performance_schema"}


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise fail("DB 식별자 오류", ErrorCode.CONFIG_INVALID)


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _approve(sha: str, summary: str, approved_sha: str, check: ApprovalCheck) -> None:
    if approved_sha != sha:
        raise fail("준비 승인 SHA 불일치", ErrorCode.PRECONDITION_FAILED)
    try:
        allowed = check(sha, summary)
    except Exception:
        raise fail("준비 승인 조회 실패", ErrorCode.PRECONDITION_FAILED) from None
    if allowed is not True:
        raise fail("준비 작업 사람 승인 필요", ErrorCode.PRECONDITION_FAILED)


@dataclass(frozen=True)
class DatabaseAccount:
    name: str
    role: Literal["app", "migrator"]
    password_env_ref: str = field(repr=False)

    def __post_init__(self) -> None:
        _identifier(self.name)
        if (
            len(self.name) > 32
            or self.role not in {"app", "migrator"}
            or not _ENV_REFERENCE.fullmatch(self.password_env_ref)
        ):
            raise fail("DB 계정 역할/환경 참조 오류", ErrorCode.CONFIG_INVALID)


@dataclass(frozen=True)
class TableObservation:
    database: str
    name: str
    # observer는 view/trigger/FK 등 교차 DB rename 불가 조건이면 False로 보고한다.
    movable: bool = True

    def __post_init__(self) -> None:
        _identifier(self.database)
        _identifier(self.name)
        if type(self.movable) is not bool:
            raise fail("DB 테이블 관측 형식 오류", ErrorCode.CONFIG_INVALID)


@dataclass(frozen=True)
class DatabaseObservation:
    target_id: str
    databases: tuple[str, ...]
    tables: tuple[TableObservation, ...]
    # 계정은 name@'%'로 고정. 다른 host 계정은 존재 증거로 인정하지 않는다.
    accounts: tuple[str, ...]
    # 계정/DB/권한의 관측. GRANT가 성공했다고 주장하는 callback만으로 성공하지 않는다.
    grants: tuple[tuple[str, str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        if not self.target_id or any(
            type(v) is not tuple for v in (self.databases, self.tables, self.accounts, self.grants)
        ):
            raise fail("DB 관측 형식 오류", ErrorCode.CONFIG_INVALID)
        for name in (*self.databases, *self.accounts):
            _identifier(name)
        if (
            len(set(self.databases)) != len(self.databases)
            or len(set(self.accounts)) != len(self.accounts)
            or any(not isinstance(t, TableObservation) for t in self.tables)
            or len({(t.database, t.name) for t in self.tables}) != len(self.tables)
            or any(t.database not in self.databases for t in self.tables)
        ):
            raise fail("DB 관측 중복/불일치", ErrorCode.CONFIG_INVALID)
        for grant in self.grants:
            if (
                type(grant) is not tuple
                or len(grant) != 3
                or grant[0] not in self.accounts
                or grant[1] not in self.databases
                or type(grant[2]) is not tuple
                or any(not re.fullmatch(r"[A-Z][A-Z ]{0,63}", p) for p in grant[2])
                or len(set(grant[2])) != len(grant[2])
            ):
                raise fail("DB 권한 관측 형식 오류", ErrorCode.CONFIG_INVALID)
        if len({g[:2] for g in self.grants}) != len(self.grants):
            raise fail("DB 권한 관측 중복", ErrorCode.CONFIG_INVALID)


@dataclass(frozen=True)
class DatabaseAction:
    kind: Literal["create_database", "rename_tables", "create_account", "grant_account"]
    database: str
    backup_database: str = ""
    tables: tuple[str, ...] = ()
    account: DatabaseAccount | None = None


class DatabasePreparationSession(Protocol):
    """전체 apply 동안 컨트롤러의 프로젝트 준비 잠금을 유지하는 main 소유 세션.

    observe는 대상 DB/backup DB 전체 테이블(앱 소유 추정 없이)과 요청한 % 계정을
    새로 조회한다. create_account는 환경 참조를 실행 경계에서만 해석하고 비밀번호를
    stdin으로 전달한다. argv/로그/반환값에는 비밀번호를 넣지 않는다.
    """

    def observe(
        self, database: str, backup_database: str, accounts: tuple[str, ...]
    ) -> DatabaseObservation: ...

    def execute(self, sql: str) -> None: ...

    def create_account(self, name: str, *, password_env_ref: str) -> None: ...


@dataclass(frozen=True)
class DatabasePreparationPlan:
    project: str
    database: str
    backup_database: str
    accounts: tuple[DatabaseAccount, ...]
    observed: DatabaseObservation = field(repr=False)
    actions: tuple[DatabaseAction, ...]

    @property
    def approval_sha(self) -> str:
        return _sha({"operation": "onprem_db_bootstrap", **asdict(self)})

    @property
    def summary(self) -> str:
        return json.dumps(
            {
                "operation": "onprem_db_bootstrap",
                "project": self.project,
                "database": self.database,
                "backup_database": self.backup_database,
                "observed_tables": sorted(
                    t.name for t in self.observed.tables if t.database == self.database
                ),
                "actions": [asdict(action) for action in self.actions],
                "preserve_data": True,
            },
            sort_keys=True,
        )


@dataclass(frozen=True)
class DatabasePreparationResult:
    approval_sha: str
    applied: tuple[DatabaseAction, ...]


def _db_plan(
    project: str,
    database: str,
    backup_database: str,
    accounts: tuple[DatabaseAccount, ...],
    observed: DatabaseObservation,
) -> DatabasePreparationPlan:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", project):
        raise fail("프로젝트 식별자 오류", ErrorCode.CONFIG_INVALID)
    for name in (database, backup_database):
        _identifier(name)
        if name.lower() in _RESERVED:
            raise fail("시스템 DB 준비 금지", ErrorCode.CONFIG_INVALID)
    if database.lower() == backup_database.lower():
        raise fail("backup DB는 별도 이름이어야 한다", ErrorCode.CONFIG_INVALID)
    if (
        type(accounts) is not tuple
        or not accounts
        or any(not isinstance(a, DatabaseAccount) for a in accounts)
        or len({a.name for a in accounts}) != len(accounts)
        or not isinstance(observed, DatabaseObservation)
        or any(t.database not in {database, backup_database} for t in observed.tables)
    ):
        raise fail("DB 준비 요청/관측 오류", ErrorCode.CONFIG_INVALID)
    existing = tuple(sorted(t.name for t in observed.tables if t.database == database))
    actions: list[DatabaseAction] = []
    if database not in observed.databases:
        actions.append(DatabaseAction("create_database", database))
    elif existing:
        # 기존 앱 소유를 추측하지 않는다. 전체 목록을 사람에게 보이고 모두 보존한다.
        if any(not t.movable for t in observed.tables if t.database == database):
            raise fail("교차 DB 이전 불가 테이블; 수동 확인 필요", ErrorCode.PRECONDITION_FAILED)
        if backup_database in observed.databases:
            raise fail("backup DB가 이미 존재한다; 덮어쓰기 금지", ErrorCode.PRECONDITION_FAILED)
        actions.extend(
            (
                DatabaseAction("create_database", backup_database),
                DatabaseAction("rename_tables", database, backup_database, existing),
            )
        )
    for account in accounts:
        if account.name not in observed.accounts:
            actions.append(DatabaseAction("create_account", database, account=account))
        actions.append(DatabaseAction("grant_account", database, account=account))
    return DatabasePreparationPlan(
        project, database, backup_database, accounts, observed, tuple(actions)
    )


def _db_observe(
    session: DatabasePreparationSession,
    database: str,
    backup_database: str,
    accounts: tuple[DatabaseAccount, ...],
) -> DatabaseObservation:
    try:
        observation = session.observe(database, backup_database, tuple(a.name for a in accounts))
        if not isinstance(observation, DatabaseObservation):
            raise ValueError("observation")
        # SQL order does not convey identity. Canonicalize the full observed inventory.
        return replace(
            observation,
            databases=tuple(sorted(observation.databases)),
            tables=tuple(sorted(observation.tables, key=lambda t: (t.database, t.name))),
            accounts=tuple(sorted(observation.accounts)),
            grants=tuple(
                sorted(
                    (name, db, tuple(sorted(privileges)))
                    for name, db, privileges in observation.grants
                )
            ),
        )
    except Exception:
        raise fail("DB 준비 상태 관측 실패", ErrorCode.PRECONDITION_FAILED) from None


def plan_db_preparation(
    *,
    project: str,
    database: str,
    backup_database: str,
    accounts: tuple[DatabaseAccount, ...],
    session: DatabasePreparationSession,
    mode: str = "BOOTSTRAP",
) -> DatabasePreparationPlan:
    """BOOTSTRAP에만 사용한다. 실행 없이 현재 전체 테이블 목록에 묶인 제안을 반환한다."""
    if mode.lower() != "bootstrap":
        raise fail("DB 준비는 BOOTSTRAP만 허용", ErrorCode.PRECONDITION_FAILED)
    # observer도 검증되지 않은 식별자를 받지 않는다.
    _db_plan(
        project, database, backup_database, accounts, DatabaseObservation("validate", (), (), ())
    )
    return _db_plan(
        project,
        database,
        backup_database,
        accounts,
        _db_observe(session, database, backup_database, accounts),
    )


def _privileges(account: DatabaseAccount) -> tuple[str, ...]:
    result = ("SELECT", "INSERT", "UPDATE", "DELETE")
    if account.role == "migrator":
        result += ("CREATE", "ALTER", "INDEX", "REFERENCES")
    return tuple(sorted(result))


def _execute_db(session: DatabasePreparationSession, action: DatabaseAction) -> None:
    if action.kind == "create_database":
        session.execute(f"CREATE DATABASE `{action.database}`")
    elif action.kind == "rename_tables":
        pairs = ", ".join(
            f"`{action.database}`.`{name}` TO `{action.backup_database}`.`{name}`"
            for name in action.tables
        )
        session.execute("RENAME TABLE " + pairs)
    elif action.account is not None:
        account = action.account
        if action.kind == "create_account":
            session.create_account(account.name, password_env_ref=account.password_env_ref)
        elif action.kind == "grant_account":
            privileges = ", ".join(_privileges(account))
            session.execute(f"GRANT {privileges} ON `{action.database}`.* TO '{account.name}'@'%'")


def _expected_db(before: DatabaseObservation, action: DatabaseAction) -> DatabaseObservation:
    if action.kind == "create_database":
        return replace(before, databases=tuple(sorted((*before.databases, action.database))))
    if action.kind == "rename_tables":
        return replace(
            before,
            tables=tuple(
                sorted(
                    (
                        replace(t, database=action.backup_database)
                        if t.database == action.database
                        else t
                        for t in before.tables
                    ),
                    key=lambda t: (t.database, t.name),
                )
            ),
        )
    if action.kind == "create_account" and action.account:
        return replace(before, accounts=tuple(sorted((*before.accounts, action.account.name))))
    if action.kind == "grant_account" and action.account:
        grants = {(name, db): set(privileges) for name, db, privileges in before.grants}
        key = (action.account.name, action.database)
        grants[key] = grants.get(key, set()) | set(_privileges(action.account))
        return replace(
            before,
            grants=tuple(
                sorted(
                    (name, db, tuple(sorted(privileges)))
                    for (name, db), privileges in grants.items()
                )
            ),
        )
    return before


def apply_db_preparation(
    plan: DatabasePreparationPlan,
    *,
    approved_sha: str,
    approval_check: ApprovalCheck,
    session: DatabasePreparationSession,
) -> DatabasePreparationResult:
    """승인·직전/직후 관측을 검증한다. 부분 실패도 DROP/자동 보상 없이 보존한다."""
    canonical = _db_plan(
        plan.project, plan.database, plan.backup_database, plan.accounts, plan.observed
    )
    if canonical != plan:
        raise fail("DB 준비 제안 변경 감지", ErrorCode.PRECONDITION_FAILED)
    _approve(plan.approval_sha, plan.summary, approved_sha, approval_check)
    expected = plan.observed
    started = False
    try:
        for action in plan.actions:
            if _db_observe(session, plan.database, plan.backup_database, plan.accounts) != expected:
                raise fail("DB 준비 중 관측 변경; 재승인 필요", ErrorCode.PRECONDITION_FAILED)
            started = True
            try:
                _execute_db(session, action)
            except Exception:
                raise fail(
                    "DB 준비 실행 실패; 상태 보존, 수동 확인 필요", ErrorCode.PRECONDITION_FAILED
                ) from None
            expected = _expected_db(expected, action)
            if _db_observe(session, plan.database, plan.backup_database, plan.accounts) != expected:
                raise fail(
                    "DB 준비 실행 상태 미확인; 수동 확인 필요", ErrorCode.PRECONDITION_FAILED
                )
    except DdakToolError as error:
        error.needs_human = started
        raise
    return DatabasePreparationResult(plan.approval_sha, plan.actions)


@dataclass(frozen=True)
class ContainerObservation:
    # secret/env 값은 절대 전달하지 않는다. config_sha는 비밀을 제외한 실행 설정의 SHA.
    target_id: str
    container_id: str
    name: str
    image_id: str
    config_sha: str
    labels: tuple[tuple[str, str], ...]
    # type/name/destination/read_write: bind는 지원하지 않는다.
    volumes: tuple[tuple[str, str, bool], ...]
    running: bool

    def __post_init__(self) -> None:
        if (
            not all((self.target_id, self.container_id, self.name, self.image_id))
            or not re.fullmatch(r"[0-9a-f]{64}", self.config_sha)
            or type(self.labels) is not tuple
            or type(self.volumes) is not tuple
            or any(type(v) is not tuple or len(v) != 2 for v in self.labels)
            or any(
                type(v) is not tuple or len(v) != 3 or type(v[2]) is not bool for v in self.volumes
            )
            or len(dict(self.labels)) != len(self.labels)
            or type(self.running) is not bool
        ):
            raise fail("컨테이너 관측 형식 오류", ErrorCode.CONFIG_INVALID)


ContainerObserver = Callable[[], ContainerObservation]
ContainerTransfer = Callable[["OwnerTransferPlan"], None]


@dataclass(frozen=True)
class OwnerTransferPlan:
    project: str
    tier: Literal["web", "was", "db"]
    observed: ContainerObservation = field(repr=False)

    @property
    def approval_sha(self) -> str:
        return _sha({"operation": "onprem_owner_transfer", **asdict(self)})

    @property
    def summary(self) -> str:
        return json.dumps(
            {
                "operation": "onprem_owner_transfer",
                "project": self.project,
                "tier": self.tier,
                "container": self.observed.name,
                "container_id": self.observed.container_id,
                "previous_owner": dict(self.observed.labels).get("ddak.project"),
                "preserve_volumes": [v[0] for v in self.observed.volumes],
                "requires_container_replacement": self.requires_replacement,
            },
            sort_keys=True,
        )

    @property
    def requires_replacement(self) -> bool:
        labels = dict(self.observed.labels)
        return any(labels.get(k) != v for k, v in self.owner_labels.items())

    @property
    def owner_labels(self) -> dict[str, str]:
        return {"ddak.managed": "true", "ddak.project": self.project, "ddak.tier": self.tier}


def _container_observe(observe: ContainerObserver) -> ContainerObservation:
    try:
        result = observe()
        if not isinstance(result, ContainerObservation):
            raise ValueError("observation")
        return replace(
            result, labels=tuple(sorted(result.labels)), volumes=tuple(sorted(result.volumes))
        )
    except Exception:
        raise fail("소유권 이전 관측 실패", ErrorCode.PRECONDITION_FAILED) from None


def _owner_plan(project: str, tier: str, observed: ContainerObservation) -> OwnerTransferPlan:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", project) or tier not in {
        "web",
        "was",
        "db",
    }:
        raise fail("소유권 이전 대상 오류", ErrorCode.CONFIG_INVALID)
    plan = OwnerTransferPlan(project, tier, observed)  # type: ignore[arg-type]
    if tier == "db" and plan.requires_replacement:
        # Docker labels are immutable. Recreating a DB to relabel violates its protection.
        raise fail(
            "NEEDS_CONTEXT: DB 소유 라벨은 재생성 없이 변경 불가; 자동 이전 금지",
            ErrorCode.PRECONDITION_FAILED,
        )
    return plan


def plan_owner_transfer(
    *,
    project: str,
    tier: str,
    observe: ContainerObserver,
) -> OwnerTransferPlan:
    return _owner_plan(project, tier, _container_observe(observe))


def apply_owner_transfer(
    plan: OwnerTransferPlan,
    *,
    approved_sha: str,
    approval_check: ApprovalCheck,
    observe: ContainerObserver,
    transfer: ContainerTransfer,
) -> ContainerObservation:
    """상태만 claim하는 callback은 실패한다. 실제 새 컨테이너·라벨·볼륨을 재관측한다."""
    _owner_plan(plan.project, plan.tier, plan.observed)
    _approve(plan.approval_sha, plan.summary, approved_sha, approval_check)
    if _container_observe(observe) != plan.observed:
        raise fail("소유권 이전 관측 변경; 재승인 필요", ErrorCode.PRECONDITION_FAILED)
    if not plan.requires_replacement:
        return plan.observed
    try:
        transfer(plan)
    except Exception:
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED,
            "소유권 이전 실패; 볼륨 보존, 수동 확인 필요",
            needs_human=True,
        ) from None
    try:
        after = _container_observe(observe)
    except DdakToolError as error:
        error.needs_human = True
        raise
    expected_labels = {**dict(plan.observed.labels), **plan.owner_labels}
    expected = replace(
        plan.observed,
        container_id=after.container_id,
        labels=tuple(sorted(expected_labels.items())),
    )
    if after.container_id == plan.observed.container_id or after != expected:
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED,
            "소유권 이전 실상태 미확인; 수동 확인 필요",
            needs_human=True,
        )
    return after
