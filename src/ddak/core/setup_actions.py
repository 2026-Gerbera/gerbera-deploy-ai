"""B6/B7 관리자 승인 조정. adapter와 Store만 주입받고 실행 모듈은 import하지 않는다."""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ddak.core.contracts.approval import ApprovalRecord
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.private_values import private_directory, read_private, write_private
from ddak.core.redact import redact

_SHA = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_RUN = re.compile(r"run-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}\Z")
_HEARTBEAT_INTERVAL = 15.0
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")


class SetupActionPlan(Protocol):
    approval_sha: str
    summary: str


class SetupActionAdapter(Protocol):
    def plan(self, kind: str, args: dict[str, Any]) -> SetupActionPlan: ...

    def apply(self, plan: SetupActionPlan, approvalcheck: Callable[[str, str], bool]) -> None: ...


@dataclass(repr=False)
class _Pending:
    adapter: SetupActionAdapter
    model: SetupActionPlan
    public: dict[str, Any]
    summary: str
    bound: str


def _failure(detail: str, code: ErrorCode = ErrorCode.PRECONDITION_FAILED) -> DdakToolError:
    return DdakToolError(code, detail)


def _bound(value: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise _failure("준비 계획 승인 해시 형식 오류")
    return value if value.startswith("sha256:") else "sha256:" + value


class SetupActions:
    def __init__(self, service: Any, factory: Callable[[str], SetupActionAdapter]) -> None:
        self.service, self.store = service, service.store
        self.root = Path(service.root) / "setup"
        self.factory = factory
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._mutex = threading.RLock()

    def _project(self, project: str) -> str:
        if not isinstance(project, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", project):
            raise _failure("프로젝트 이름 형식 오류", ErrorCode.CONFIG_INVALID)
        result = self.service.resolve_project(project)
        if not isinstance(result, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", result):
            raise _failure("프로젝트 이름 형식 오류", ErrorCode.CONFIG_INVALID)
        return result

    def _folder(self, project: str) -> Path:
        return self.root / project / "actions"

    def _persist(self, project: str, public: dict[str, Any]) -> None:
        with private_directory(self._folder(project), create=True) as fd:
            write_private(
                fd, public["id"] + ".json", json.dumps(public, ensure_ascii=False).encode()
            )

    @staticmethod
    def _arguments(kind: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(args, Mapping):
            raise _failure("준비 입력 형식 오류", ErrorCode.CONFIG_INVALID)
        if kind == "database":
            keys = {"database", "backup_database", "app_account", "migrator_account"}
            if set(args) != keys or any(
                not isinstance(value, str) or not _NAME.fullmatch(value) for value in args.values()
            ):
                raise _failure("DB·계정 이름 입력 형식 오류", ErrorCode.CONFIG_INVALID)
            if (
                args["database"] == args["backup_database"]
                or args["app_account"] == args["migrator_account"]
            ):
                raise _failure("DB와 백업 DB, 앱과 마이그레이터 계정은 각각 달라야 한다")
        elif kind == "ownership":
            if args.get("tier") == "db":
                raise _failure(
                    "NEEDS_CONTEXT: DB 소유 라벨은 변경할 수 없어 담당자 확인이 필요하다"
                )
            if (
                set(args) != {"tier", "replica"}
                or args.get("tier") not in {"web", "was"}
                or type(args.get("replica")) is not int
                or not 1 <= args["replica"] <= 100
            ):
                raise _failure("소유권 이전 tier·replica 형식 오류", ErrorCode.CONFIG_INVALID)
        else:
            raise _failure("지원하지 않는 준비 종류", ErrorCode.CONFIG_INVALID)
        return dict(args)

    def plan(self, project: str, kind: str, args: Mapping[str, Any]) -> dict[str, Any]:
        project = self._project(project)
        arguments = self._arguments(kind, args)
        with self._mutex:
            self.store.assert_idle(project)
            try:
                adapter = self.factory(project)
                model = adapter.plan(kind, arguments)
                bound = _bound(model.approval_sha)
                if (
                    not isinstance(model.summary, str)
                    or not model.summary
                    or len(model.summary) > 24000
                ):
                    raise _failure("준비 계획 요약 형식 오류")
                summary = model.summary
            except DdakToolError as exc:
                raise _failure(redact(str(exc), max_len=500)) from None
            except Exception:
                raise _failure(
                    "준비 계획을 관측할 수 없다. 연결·소유권·입력을 확인하세요"
                ) from None
            ident = datetime.now(UTC).strftime("run-%Y%m%d-%H%M%S-") + secrets.token_hex(2)
            public = {
                "id": ident,
                "project": project,
                "kind": kind,
                "hash": model.approval_sha,
                "summary": redact(summary, max_len=24000),
                "status": "AWAITING_APPROVAL",
                "detail": "변경 요약과 해시를 확인한 뒤 승인하세요",
            }
            # 동일 프로젝트의 이전 후보는 더 이상 승인할 수 없다.
            for key, old in list(self._pending.items()):
                if key[0] == project:
                    previous = {
                        **old.public,
                        "status": "SUPERSEDED",
                        "detail": "새 준비 계획으로 대체됨",
                    }
                    self._persist(project, previous)
                    del self._pending[key]
            self._persist(project, public)
            self._pending[project, ident] = _Pending(adapter, model, public, summary, bound)
            return dict(public)

    def view(self, project: str) -> dict[str, Any]:
        project = self._project(project)
        with self._mutex:
            entries = []
            try:
                with private_directory(self._folder(project)) as fd:
                    for name in sorted(os.listdir(fd)):
                        if not name.endswith(".json") or not _RUN.fullmatch(name[:-5]):
                            continue
                        public = json.loads(read_private(fd, name))
                        if (
                            not isinstance(public, dict)
                            or public.get("project") != project
                            or public.get("id") != name[:-5]
                        ):
                            raise _failure("준비 감사 요약 형식 오류")
                        try:
                            stored = self.store.run(public["id"])
                        except KeyError:
                            stored = None
                        if stored is not None and stored["status"] != "AWAITING_APPROVAL":
                            public["status"] = stored["status"]
                            finished = (stored.get("result") or {}).get("setup_operation", {})
                            public["detail"] = finished.get("detail") or (
                                "환경 상태를 사람이 확인해야 합니다"
                                if stored["status"] == "NEEDS_HUMAN"
                                else "Store 실행 기록을 확인하세요"
                            )
                        if (
                            public.get("status") == "AWAITING_APPROVAL"
                            and (project, public["id"]) not in self._pending
                        ):
                            public["status"] = "REPLAN_REQUIRED"
                            public["detail"] = "서비스가 재시작되어 다시 계획하고 승인해야 합니다"
                        entries.append(public)
            except FileNotFoundError:
                pass
            return {"project": project, "actions": entries}

    def apply(self, project: str, id: str, approved_hash: str, actor: str) -> dict[str, Any]:
        project = self._project(project)
        if not isinstance(id, str) or not _RUN.fullmatch(id):
            raise _failure("준비 승인 식별자 형식 오류", ErrorCode.CONFIG_INVALID)
        if not isinstance(actor, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", actor):
            raise _failure("승인자 형식 오류", ErrorCode.CONFIG_INVALID)
        with self._mutex:
            pending = self._pending.get((project, id))
            if pending is None:
                raise _failure(
                    "이 준비 계획은 사용할 수 없다. 재시작·중복 승인 뒤에는 재계획이 필요하다"
                )
            if (
                approved_hash != pending.public["hash"]
                or _bound(pending.model.approval_sha) != pending.bound
                or pending.model.summary != pending.summary
            ):
                raise _failure("승인 해시 또는 준비 관측 요약이 바뀌었다. 재계획이 필요하다")
            self.store.assert_idle(project)
            # acquire 실패에도 동일 요청을 재실행하지 않는다.
            del self._pending[project, id]
            self.store.create_run(id, project, pending.bound)
            record = ApprovalRecord(
                run_id=id,
                project=project,
                approval_id=secrets.token_hex(16),
                kind="deploy",
                bound_to=pending.bound,
                approver=actor,
                approved_at=datetime.now(UTC),
                decision="approved",
            )
            self.store.approve([record])
            try:
                token = self.store.acquire(project, id, targets="local")
            except Exception:
                self.store.mark_stopped(id, "CANCELLED")
                self._persist(
                    project,
                    {
                        **pending.public,
                        "status": "CANCELLED",
                        "detail": "프로젝트 잠금 획득 실패; 재계획 필요",
                    },
                )
                raise _failure(
                    "다른 실행이 진행 중이거나 환경 확인이 필요하다", ErrorCode.LOCK_HELD
                ) from None

        stop = threading.Event()
        lost = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(_HEARTBEAT_INTERVAL):
                try:
                    self.store.heartbeat(project, id, token)
                except Exception:
                    lost.set()
                    return

        def approvalcheck(sha: str, summary: str) -> bool:
            if lost.is_set() or sha != pending.public["hash"] or summary != pending.summary:
                return False
            try:
                self.store.check_lock(project, id, token)
                run = self.store.run(id)
                return (
                    run["status"] == "RUNNING"
                    and run["plan_hash"] == pending.bound
                    and any(
                        r.project == project
                        and r.bound_to == pending.bound
                        and r.approval_id == record.approval_id
                        and r.approver == actor
                        and r.decision == "approved"
                        for r in self.store.approvals(id)
                    )
                )
            except Exception:
                return False

        worker = threading.Thread(target=heartbeat, daemon=True, name="setup-heartbeat")
        worker.start()
        started = False
        result: dict[str, Any] = {}
        try:
            self._persist(
                project,
                {
                    **pending.public,
                    "status": "RUNNING",
                    "actor": actor,
                    "detail": "승인된 준비 작업 실행 중",
                },
            )
            self.store.heartbeat(project, id, token)
            if not approvalcheck(pending.public["hash"], pending.summary):
                raise _failure("준비 실행의 저장된 승인·잠금 검증 실패")
            started = True
            pending.adapter.apply(pending.model, approvalcheck)
            if not approvalcheck(pending.public["hash"], pending.summary):
                lost.set()
                raise _failure("준비 실행 뒤 승인·잠금 확인 실패")
            result = {
                **pending.public,
                "status": "SUCCEEDED",
                "actor": actor,
                "detail": "관리자 준비 작업 완료. 배포 성공·애플리케이션 정상 확인은 별도입니다",
            }
            self._persist(project, result)
            self.store.finish(id, "SUCCEEDED", {"setup_operation": result}, {}, {})
            return dict(result)
        except BaseException as exc:
            # 전송/부분 실행 여부가 불명인 일반 예외도 사람 확인으로 보수적으로 차단한다.
            uncertain = bool(getattr(exc, "needs_human", started)) or lost.is_set()
            status = "NEEDS_HUMAN" if uncertain else "CANCELLED"
            detail = (
                redact(str(exc), max_len=500)
                if isinstance(exc, DdakToolError)
                else "준비 실행 실패; 원문 출력은 숨김"
            )
            result = {**pending.public, "status": status, "detail": detail, "actor": actor}
            try:
                self.store.finish(id, status, {"setup_operation": result}, {}, {})
            finally:
                self.store.mark_stopped(id, "NEEDS_HUMAN" if uncertain else "CANCELLED")
                self._persist(project, result)
            error = _failure(detail)
            error.needs_human = uncertain
            raise error from None
        finally:
            stop.set()
            worker.join(timeout=2)
            self.store.release(project, id, token)
