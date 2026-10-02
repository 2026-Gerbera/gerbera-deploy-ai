"""C3/O2 진입점: prepare → approval_view → approve → start/wait.

웹 인증/CSRF는 C3 책임이다. 이 객체는 신뢰된 컨트롤러 내부 API이며 HTTP에 그대로 노출하지 않는다.
팀 툴은 레지스트리로만 호출한다. 사실 재계산은 O2가 주입하는 facts_reader를 사용한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal, cast

from ddak.core.app_repository import AppRepository
from ddak.core.config import AdapterMode
from ddak.core.contracts.approval import ApprovalRecord
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.enums import Effect, Layer, RunMode, Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.events import EventType, RunEvent
from ddak.core.contracts.infra_outputs import (
    APP_OUTPUTS,
    APP_SECRET_OUTPUT,
    PLATFORM_OUTPUTS,
    checked_cloud_outputs,
)
from ddak.core.contracts.plan import Plan, PlanStep
from ddak.core.contracts.release import (
    ImageObservation,
    ReleaseArtifacts,
    SnapshotBinding,
)
from ddak.core.project_settings import ProjectSettings
from ddak.core.redact import redact, redact_obj
from ddak.core.registry import Registry, UnknownToolError
from ddak.core.runlog import run_dir, write_context
from ddak.core.snapshots import digest_bytes, digest_json, file_manifest, materialize, preview
from ddak.core.store import Store, release_view
from ddak.executor.approval_meta import check_infra_summary, encode_meta
from ddak.executor.engine import Executor, RunResult, RunStatus, TrackStatus, check_signals
from ddak.executor.events import EventBus
from ddak.executor.images import carried_image_source, carried_images, locked_database
from ddak.executor.selection import select_plan

ApprovalKind = Literal["patch", "deploy", "infra", "dockerfile", "foundation"]
FactsReader = Callable[[Path], str]
ContextRefresh = Callable[[PlanStep, dict[str, Any], RunContext], RunContext]
PlanningFlow = Callable[["DeploymentService", DeployRequest], Awaitable[str]]
_HEARTBEAT_INTERVAL_S = 10.0


def plan_digest(plan: Plan) -> str:
    return digest_json(plan.model_dump(mode="json", by_alias=True, exclude={"plan_hash"}))


def source_facts(root: Path) -> str:
    return digest_json(file_manifest(root))


def environment_release(
    release: dict[str, Any], previous: Mapping[str, Any], target: str
) -> dict[str, Any]:
    """환경 장부는 미변경 tier를 보존한다. 새 빌드 snapshot에 이전 산출물을 섞지 않는다."""
    images = {**previous.get("images", {}), **release["images"]}
    origins = {}
    for tier in images:
        current = tier in release["images"]
        origin = release if current else previous
        inherited = (previous.get("image_sources") or {}).get(tier)
        artifacts = origin.get("artifacts") or {}
        origins[tier] = {
            **(
                inherited
                if not current and inherited
                else {
                    "release_id": origin.get("release_id"),
                    "source_sha": origin.get("source_sha"),
                    "candidate_sha": origin.get("candidate_sha"),
                    "snapshot": origin.get("source"),
                    "artifact": artifacts.get("images", {}).get(tier),
                    "observation": artifacts.get("observations", {}).get(target, {}).get(tier),
                }
            ),
            "carried_forward": not current,
        }
    return {**release, "images": images, "image_sources": origins}


def platform_outputs(context: RunContext) -> dict[str, Any]:
    try:
        return checked_cloud_outputs(
            {
                k: v
                for k, v in context.platform.get("cloud", {}).items()
                if k in PLATFORM_OUTPUTS or k in APP_OUTPUTS or APP_SECRET_OUTPUT.fullmatch(k)
            }
        )
    except (ValueError, TypeError, AttributeError):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "플랫폼 출력 형식 오류") from None


@dataclass(frozen=True)
class PreparedRun:
    plan: Plan
    context: RunContext
    source: Path
    snapshot: SnapshotBinding
    patch: bytes | None
    requirements: Mapping[ApprovalKind, str]
    facts_reader: FactsReader
    context_hash: str
    source_files: dict[str, dict[str, Any]]
    patch_meta_json: str
    infra_summary_json: str


class DeploymentService:
    """프로젝트별 실행 잠금 + 컨트롤러 1개. 화면 연결 수명과 실행 task 수명은 분리한다."""

    def __init__(
        self,
        registry: Registry,
        root: Path,
        *,
        refresh: ContextRefresh | None = None,
        repositories: Mapping[str, AppRepository] | None = None,
        facts_readers: Mapping[str, FactsReader] | None = None,
        planning_flow: PlanningFlow | None = None,
        repository_factory: Callable[[RunContext], AppRepository] | None = None,
    ) -> None:
        root = root.expanduser().resolve()
        self.registry, self.root, self.refresh = registry, root, refresh
        self.repositories = dict(repositories or {})
        self.repository_factory = repository_factory
        self.planning_flow = planning_flow
        root.mkdir(parents=True, exist_ok=True)
        self._lease = (root / "controller.lock").open("a")
        try:
            fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lease.close()
            raise DdakToolError(
                ErrorCode.LOCK_HELD, "같은 상태 저장소의 컨트롤러가 이미 실행 중이다"
            ) from None
        self.store = Store(root / "ddak.sqlite")
        self.interrupted = self.store.recover_interrupted()
        self._facts_readers = {"source_manifest": source_facts, **(facts_readers or {})}
        self._tasks: dict[str, asyncio.Task[RunResult]] = {}
        self._buses: dict[str, EventBus] = {}
        self._entered: set[str] = set()
        self._tokens: dict[str, str] = {}

    def close(self) -> None:
        if any(not t.done() for t in self._tasks.values()):
            raise RuntimeError("실행 중인 배포가 있다")
        self._lease.close()

    async def shutdown(self) -> None:
        pending = {run_id: task for run_id, task in self._tasks.items() if not task.done()}
        for run_id, task in pending.items():
            if run_id not in self._entered:
                # create_task 이후 첫 실행 전 취소는 대상 변경이 없으므로 수동 복구가 필요 없다.
                task.cancel()
                project = self.store.run(run_id)["project"]
                self.store.mark_stopped(run_id, "CANCELLED")
                self.store.release(project, run_id, self._tokens[run_id])
            else:
                task.cancel()
        if pending:
            await asyncio.gather(*pending.values(), return_exceptions=True)
        self.close()

    def connect_repository(self, context: RunContext) -> AppRepository | None:
        """조립부가 제공한 factory는 승인 입력을 사용해 재시작 후에도 같은 checkout을 연결한다."""
        repository = (
            self.repository_factory(context)
            if self.repository_factory is not None
            else self.repositories.get(context.project)
        )
        if repository is not None and context.repo_url:
            repository.require_origin(context.repo_url)
        return repository

    def prepare(
        self,
        plan: Plan,
        context: RunContext,
        source: Path,
        *,
        patch: bytes | None = None,
        subjects: Mapping[ApprovalKind, str] | None = None,
        facts_reader: FactsReader | None = None,
        patch_meta: dict[str, Any] | None = None,
        infra_summary: dict[str, Any] | None = None,
        expected_settings_version: int | None = None,
    ) -> str:
        if source.expanduser().is_symlink():
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "소스 심볼릭 링크는 지원하지 않는다")
        source = source.expanduser().resolve()
        if context.source_binding is not None:
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "source_binding은 승인 뒤 실행기만 채운다"
            )
        platform_outputs(context)
        settings = self.store.project_settings(context.project)
        if expected_settings_version is not None and (
            (settings or {}).get("version", 0) != expected_settings_version
        ):
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "계획 중 프로젝트 설정이 변경됐다")
        if settings is not None:
            settings_data = {k: v for k, v in settings.items() if k in ProjectSettings.model_fields}
            validated = ProjectSettings.model_validate(settings_data)
            context = replace(
                context,
                project_settings={
                    **validated.model_dump(mode="json", exclude_unset=True),
                    "version": settings["version"],
                },
                cloud_domain=validated.cloud_domain or context.cloud_domain,
            )
        if context.repo_url and context.project_settings.get("repo_url") not in (
            None,
            context.repo_url,
        ):
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "요청 저장소와 프로젝트 설정이 다르다"
            )
        plan = select_plan(plan, context)
        if context.adapter_mode is AdapterMode.REAL and plan.deploy.local.steps:
            inventory = context.platform.get("onprem")
            tiers = inventory.get("tiers") if isinstance(inventory, Mapping) else None
            if not isinstance(tiers, Mapping) or not tiers:
                raise DdakToolError(
                    ErrorCode.CONFIG_INVALID, "실제 온프렘 배포 인벤토리가 필요하다"
                )
            needed = {s.tier for s in plan.deploy.local.steps if s.tool == "deploy_tier" and s.tier}
            needed |= {s.tier or "was" for s in plan.deploy.local.steps if s.tool == "prepare_db"}
            missing = needed - tiers.keys()
            if missing:
                raise DdakToolError(
                    ErrorCode.CONFIG_INVALID,
                    "온프렘 인벤토리 tier 누락: " + ", ".join(sorted(missing)),
                )
        snapshot = preview(source, patch)
        if context.release_artifacts and context.release_artifacts.snapshot != snapshot:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "입력 이미지의 소스 결합이 다르다")
        database_deploy = any(
            s.tool == "deploy_tier" and s.tier == "db" for s in plan.deploy.local.steps
        )
        if database_deploy and any(
            s.tool == "deploy_tier" and s.tier == "db" for s in plan.deploy.cloud.steps
        ):
            raise DdakToolError(
                ErrorCode.PLAN_INVALID,
                "온프렘 MySQL은 cloud db 배포에 공유할 수 없다; 클라우드 DB 계획 연결 필요",
            )
        if database_deploy and any(
            s.tool == "build_image" and s.tier == "db" for s in plan.build.steps
        ):
            raise DdakToolError(
                ErrorCode.PLAN_INVALID,
                "온프렘 db는 공식 이미지 사용: O2 계획에서 build.db를 제외해야 한다",
            )
        if database_deploy and not (
            context.release_artifacts and "db" in context.release_artifacts.images
        ):
            db_image = locked_database(source, snapshot, patch)
            supplied_images = (
                dict(context.release_artifacts.images) if context.release_artifacts else {}
            )
            context = replace(
                context,
                images={**context.images, "db": db_image.ref},
                release_artifacts=ReleaseArtifacts(
                    snapshot=snapshot, images={**supplied_images, "db": db_image}
                ),
            )
        if (
            database_deploy
            and context.release_artifacts
            and not context.release_artifacts.images["db"].ref.startswith(
                ("mysql@", "docker.io/library/mysql@")
            )
        ):
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "DB는 digest 고정 공식 MySQL 이미지만 허용한다"
            )
        previous = {
            target: row["current"]
            for target, row in self.store.environments(context.project).items()
            if row["current"] and row["status"] != "NEEDS_HUMAN"
        }
        supplied = context.release_artifacts
        carried = carried_images(
            plan, previous, context.adapter_mode, supplied.images if supplied else ()
        )
        if any(set(context.images) & set(tiers) for tiers in carried.values()):
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "미빌드 tier 이미지는 환경의 성공 기록만 사용한다"
            )
        context = replace(
            context,
            previous_release={target: previous[target] for target in carried},
            images={
                **({t: a.ref for t, a in supplied.images.items()} if supplied else {}),
                **context.images,
            },
        )
        patch_meta_json = encode_meta(patch_meta)
        infra_summary_json = encode_meta(infra_summary, infra=True)
        if (patch_meta is not None and not patch) or (
            infra_summary is not None and not (subjects or {}).get("infra")
        ):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "승인 메타에 대응하는 대상이 없다")
        if not context.project or (plan.run_id, plan.project, plan.mode) != (
            context.run_id,
            context.project,
            context.mode,
        ):
            raise DdakToolError(ErrorCode.PLAN_INVALID, "계획과 컨텍스트 식별자가 다르다")
        directory = run_dir(self.root / "runs", plan.run_id)
        if plan.toggles != dict(context.toggles):
            raise DdakToolError(ErrorCode.PLAN_INVALID, "계획과 컨텍스트 토글이 다르다")
        if patch and not context.toggles.get("code_patch"):
            raise DdakToolError(ErrorCode.TOGGLE_OFF, "코드 수정이 꺼져 있다")
        check_signals(plan)
        steps = [
            s
            for section in (plan.build, plan.deploy.local, plan.deploy.cloud, plan.verify)
            for s in section.steps
        ]
        for step in steps:
            spec = self.registry.get(step.tool).spec
            if spec.layer in {Layer.BUILTIN, Layer.OUTSIDE}:
                raise DdakToolError(
                    ErrorCode.PLAN_INVALID, "내장/계획 밖 툴은 계획에서 실행할 수 없다"
                )
        if facts_reader is None:
            # O2가 아직 연결되지 않은 로컬 호출의 명시적인 기본 facts = 소스 manifest 해시.
            facts_reader = source_facts

        facts_hash = facts_reader(source)
        if plan.facts_hash is not None and plan.facts_hash != facts_hash:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "facts가 변경됐다. 재계획이 필요하다"
            )
        plan = Plan.model_validate(
            plan.model_dump(mode="json", by_alias=True) | {"facts_hash": facts_hash}
        )
        if plan.plan_hash is not None and plan.plan_hash != plan_digest(plan):
            raise DdakToolError(ErrorCode.PLAN_INVALID, "plan_hash가 계획과 다르다")
        plan = plan.model_copy(update={"plan_hash": plan_digest(plan)})
        required: dict[ApprovalKind, str] = dict(subjects or {})
        required["deploy"] = cast(str, plan.plan_hash)
        if snapshot.patch_sha256:
            required["patch"] = snapshot.patch_sha256
        if any(s.tool == "apply_infra" for s in steps) and "infra" not in required:
            raise DdakToolError(ErrorCode.APPROVAL_REQUIRED, "인프라 plan 해시가 필요하다")
        if any(s.tool == "apply_infra" for s in steps) and self.refresh is None:
            raise DdakToolError(ErrorCode.INFRA_MISSING, "인프라 출력 갱신 연결이 필요하다")
        if context.adapter_mode is AdapterMode.REAL:
            for target, section in (("local", plan.deploy.local), ("cloud", plan.deploy.cloud)):
                expected_signal = f"{target}_verified"
                if section.steps and not (
                    section.signal == expected_signal
                    or any(step.signal == expected_signal for step in section.steps)
                ):
                    raise DdakToolError(ErrorCode.PLAN_INVALID, "실배포에 검증 완료 신호가 없다")
                if section.steps and not {"health_check", "smoke_test"}.issubset(
                    {s.tool for s in section.steps}
                ):
                    raise DdakToolError(
                        ErrorCode.PLAN_INVALID, "실배포에는 헬스·스모크 검사가 필요하다"
                    )
                opened = False
                for step in section.steps:
                    if opened and self.registry.spec(step.tool).effect is Effect.STATE_CHANGE:
                        raise DdakToolError(
                            ErrorCode.PLAN_INVALID, "검증 신호 뒤에 상태 변경이 있다"
                        )
                    opened = opened or step.signal in {"local_verified", "cloud_verified"}
        # ApprovalRecord의 SHA 검증을 승인 전에도 적용한다.
        for kind, digest in required.items():
            ApprovalRecord(
                run_id=plan.run_id,
                project=plan.project,
                approval_id="preview",
                kind=kind,
                bound_to=digest,
                approver="preview",
                approved_at=datetime.now(UTC),
                decision="denied",
                snapshot=snapshot if kind == "patch" else None,
            )
        context = replace(
            context,
            deploy_config=json.loads(json.dumps(context.deploy_config)),
            platform=json.loads(json.dumps(context.platform)),
            images=dict(context.images),
            toggles=dict(context.toggles),
            project_settings=json.loads(json.dumps(context.project_settings)),
        )
        self.store.create_run(plan.run_id, plan.project, cast(str, plan.plan_hash))
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "plan.json").write_text(plan.model_dump_json(by_alias=True, indent=2) + "\n")
        if patch:
            (directory / "approved.patch").write_bytes(patch)
        (directory / "approval-meta.json").write_text(
            json.dumps(
                {
                    "patch_meta": json.loads(patch_meta_json),
                    "infra_summary": json.loads(infra_summary_json),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        reader_key = next(
            (key for key, reader in self._facts_readers.items() if reader is facts_reader),
            f"custom:{plan.run_id}",
        )
        self._facts_readers[reader_key] = facts_reader
        self.store.save_prepared(
            plan.run_id,
            {
                "plan": plan.model_dump(mode="json", by_alias=True),
                "context": context.to_json_dict(),
                "source": str(source.resolve()),
                "snapshot": snapshot.model_dump(mode="json"),
                "patch": patch.decode("utf-8") if patch else None,
                "requirements": required,
                "facts_reader": reader_key,
                "context_hash": digest_json(context.to_json_dict()),
                "source_files": file_manifest(source),
                "patch_meta_json": patch_meta_json,
                "infra_summary_json": infra_summary_json,
            },
        )
        write_context(self.root / "runs", plan.run_id, context.to_json_dict())
        (directory / "approval-view.json").write_text(
            json.dumps(self.approval_view(plan.run_id), ensure_ascii=False)
        )
        self._buses[plan.run_id] = EventBus()
        return plan.run_id

    def _load_prepared(self, run_id: str) -> PreparedRun:
        data = self.store.prepared(run_id)
        if data is None:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "복원 가능한 승인 대기 입력이 없다")
        reader = self._facts_readers.get(data["facts_reader"])
        if reader is None:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED,
                "원래 facts_reader를 조립 지점에서 다시 주입해야 한다",
            )
        context_data = dict(data["context"])
        context_data["adapter_mode"] = AdapterMode(context_data["adapter_mode"])
        context_data["mode"] = RunMode(context_data["mode"])
        if context_data.get("release_artifacts"):
            context_data["release_artifacts"] = ReleaseArtifacts.model_validate(
                context_data["release_artifacts"]
            )
        p = PreparedRun(
            plan=Plan.model_validate(data["plan"]),
            context=RunContext(**context_data),
            source=Path(data["source"]),
            snapshot=SnapshotBinding.model_validate(data["snapshot"]),
            patch=data["patch"].encode("utf-8") if data["patch"] else None,
            requirements=data["requirements"],
            facts_reader=reader,
            context_hash=data["context_hash"],
            source_files=data["source_files"],
            patch_meta_json=data["patch_meta_json"],
            infra_summary_json=data["infra_summary_json"],
        )
        row = self.store.run(run_id)
        if (
            p.plan.run_id != run_id
            or p.context.run_id != run_id
            or p.plan.project != row["project"]
            or p.context.project != row["project"]
            or plan_digest(p.plan) != row["plan_hash"]
            or p.plan.plan_hash != row["plan_hash"]
            or p.requirements.get("deploy") != row["plan_hash"]
            or digest_json(p.context.to_json_dict()) != p.context_hash
            or (digest_bytes(p.patch) if p.patch else None) != p.snapshot.patch_sha256
            or p.requirements.get("patch") != p.snapshot.patch_sha256
            or digest_json(p.source_files) != p.snapshot.source_snapshot_hash
        ):
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "저장된 승인 입력의 해시가 다르다")
        self._buses.setdefault(run_id, EventBus())
        return p

    @staticmethod
    async def _repository_work(
        function: Any,
        *args: Any,
        complete_on_cancel: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> Any:
        work = asyncio.create_task(asyncio.to_thread(function, *args))
        cancelled = False
        while True:
            try:
                value = await asyncio.shield(work)
                break
            except asyncio.CancelledError:
                if work.cancelled():
                    raise
                cancelled = True
                if cancel_event is not None:
                    cancel_event.set()
        if cancelled:
            if not complete_on_cancel:
                raise asyncio.CancelledError
            if isinstance(value, dict):
                value = {**value, "cancel_requested": True}
        return value

    def deployment_baselines(self, project: str) -> dict[str, str | None]:
        return {
            target: row["current"].get("source_sha") if row["current"] else None
            for target, row in self.store.environments(project).items()
        }

    def record_preparation_failure(
        self,
        run_id: str,
        project: str,
        error: Exception,
        *,
        context: RunContext | None = None,
        phase: str = "prepare",
        plan: Plan | None = None,
    ) -> bool:
        # 외부 예외 문자열에는 저장소 URL·소스·자격증명이 있을 수 있다.
        result = {
            "phase": phase,
            "code": error.code.value if isinstance(error, DdakToolError) else "INTERNAL",
            "error_type": type(error).__name__,
        }
        if isinstance(error, DdakToolError):
            result["detail"] = redact(error.message)
        elif isinstance(error, UnknownToolError) and error.args:
            result["code"] = ErrorCode.CONFIG_INVALID.value
            result["detail"] = "계획에 필요한 툴이 등록되지 않았다"
            name = str(error.args[0])
            if re.fullmatch(r"[a-z][a-z0-9_]*", name):
                result["missing_tool"] = name
        saved = self.store.preparation_failed(run_id, project, result)
        if saved and context is not None:
            write_context(self.root / "runs", run_id, context.to_json_dict())
        if saved and plan is not None:
            directory = run_dir(self.root / "runs", run_id)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "plan.json").write_text(
                json.dumps(
                    redact_obj(plan.model_dump(mode="json", by_alias=True)),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
        return saved

    def get_run(self, run_id: str) -> dict[str, Any]:
        result = self.store.run(run_id)
        path = run_dir(self.root / "runs", run_id) / "context.json"
        if path.exists():
            context = json.loads(path.read_text())
            result["context"] = {
                k: context.get(k)
                for k in (
                    "targets",
                    "trigger",
                    "source_sha",
                    "candidate_sha",
                    "project_settings",
                    "cloud_domain",
                    "repo_url",
                    "ref",
                )
            }
        return result

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.list_runs(limit)

    def get_environments(self, project: str) -> dict[str, Any]:
        return self.store.environments(project)

    def get_release(self, run_id: str) -> dict[str, Any] | None:
        self.store.run(run_id)
        return self.store.release_record(run_id)

    def get_approvals(self, run_id: str) -> list[ApprovalRecord]:
        self.store.run(run_id)
        return self.store.approvals(run_id)

    def list_project_settings(self) -> list[dict[str, Any]]:
        return self.store.list_project_settings()

    def get_project_settings(self, project: str) -> dict[str, Any] | None:
        return self.store.project_settings(project)

    def get_platform_outputs(self, project: str, mode: AdapterMode) -> dict[str, Any]:
        return checked_cloud_outputs(self.store.platform_outputs(project, mode.value))

    def guard_infra(self, run_id: str, project: str) -> None:
        """승인 전에는 조회만 가능하고, 실행 중에는 해당 실행의 잠금을 확인한다."""
        try:
            row = self.store.run(run_id)
        except KeyError:
            self.store.assert_idle(project)
            return
        if row["project"] != project or row["status"] != "RUNNING":
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "인프라 실행 상태가 다르다")
        self.store.check_lock(project, run_id, self._tokens.get(run_id))

    async def request_deployment(
        self,
        project: str,
        *,
        targets: Literal["onprem", "cloud", "both"] | None = None,
        ref: str | None = None,
    ) -> str:
        """수동 요청 → 주입된 계획 흐름 → 승인 대기 run_id. 승인·실행은 별도 동작이다."""
        data = self.store.project_settings(project) or {}
        settings = ProjectSettings.model_validate(
            {k: v for k, v in data.items() if k in ProjectSettings.model_fields}
        )
        if not settings.repo_url or self.planning_flow is None:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 설정과 계획 흐름 연결이 필요하다")
        chosen = targets if targets is not None else settings.default_targets
        if chosen not in {"onprem", "cloud", "both"}:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "targets는 onprem/cloud/both만 허용한다")
        ref = settings.watch_branch if ref is None else ref
        if ref != settings.watch_branch:
            tag = ref.removeprefix("refs/tags/")
            if (
                not re.fullmatch(r"v[A-Za-z0-9._/-]{0,189}", tag)
                or any(part in tag for part in ("..", "//", "@{"))
                or tag.endswith(("/", ".", ".lock"))
            ):
                raise DdakToolError(
                    ErrorCode.CONFIG_INVALID, "감시 브랜치 또는 v* 태그만 배포할 수 있다"
                )
            ref = "refs/tags/" + tag  # 같은 이름의 브랜치가 있어도 태그만 선택한다.
        request = DeployRequest(
            project=project,
            repo_url=settings.repo_url,
            ref=ref,
            target="local" if chosen == "onprem" else chosen,
        )
        return await self.planning_flow(self, request)

    def save_project_settings(
        self, project: str, data: dict[str, Any], *, updated_by: str, expected_version: int | None
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", project):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "프로젝트 이름 형식 오류")
        if expected_version is None or isinstance(expected_version, bool) or expected_version < 0:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "설정 저장에는 읽은 버전이 필요하다")
        existing = self.store.project_settings(project) or {}
        merged = {k: v for k, v in existing.items() if k in ProjectSettings.model_fields}
        merged.update(data)
        validated = ProjectSettings.model_validate(merged)
        return self.store.save_project_settings(
            project,
            validated.model_dump(mode="json", exclude_unset=True),
            updated_by=updated_by,
            expected_version=expected_version,
        )

    def approval_view(self, run_id: str) -> dict[str, Any]:
        row = self.store.run(run_id)
        if row["status"] == "FAILED_BEFORE_DEPLOY" and (row.get("result") or {}).get("phase"):
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED,
                "준비에 실패하여 승인할 수 없다. get_run의 result에서 실패 원인을 확인한다",
            )
        if self.store.prepared(run_id) is None:
            self.store.run(run_id)
            directory = run_dir(self.root / "runs", run_id)
            saved = directory / "approval-view.json"
            if saved.exists():
                return json.loads(saved.read_text())
            # 신규 export가 없는 기존 실행은 원본 기록에서 조회용으로만 복원한다.
            records = self.store.approvals(run_id)
            release = self.store.release_record(run_id) or {}
            metadata_path = directory / "approval-meta.json"
            metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
            plan_path = directory / "plan.json"
            restored_plan = json.loads(plan_path.read_text()) if plan_path.exists() else None
            patch_path = directory / "approved.patch"
            snapshot = next(
                (r.snapshot.model_dump(mode="json") for r in records if r.snapshot),
                release.get("source"),
            )
            return {
                "run_id": run_id,
                "project": self.store.run(run_id)["project"],
                "subjects": {r.kind: r.bound_to for r in records},
                "snapshot": snapshot,
                "patch": patch_path.read_text() if patch_path.exists() else None,
                "plan": restored_plan,
                "patch_meta": metadata.get("patch_meta"),
                "infra_summary": metadata.get("infra_summary"),
                "legacy_record": True,
                "unavailable_fields": [
                    name
                    for name, value in (("snapshot", snapshot), ("plan", restored_plan))
                    if value is None
                ],
            }

        p = self._load_prepared(run_id)
        return {
            "run_id": run_id,
            "project": p.plan.project,
            "targets": p.context.targets,
            "trigger": p.context.trigger,
            "repo_url": p.context.repo_url,
            "ref": p.context.ref,
            "project_settings": dict(p.context.project_settings),
            "subjects": dict(p.requirements),
            "snapshot": p.snapshot.model_dump(mode="json"),
            "patch": p.patch.decode() if p.patch else None,
            "plan": p.plan.model_dump(mode="json", by_alias=True),
            "patch_meta": json.loads(p.patch_meta_json),
            "infra_summary": json.loads(p.infra_summary_json),
        }

    def approve(self, run_id: str, *, approver: str, approved: bool = True) -> list[ApprovalRecord]:
        p = self._load_prepared(run_id)
        if approved:
            self._check_meta(p)
        approval_id, now = uuid.uuid4().hex, datetime.now(UTC)
        records = [
            ApprovalRecord(
                run_id=run_id,
                project=p.plan.project,
                approval_id=approval_id,
                kind=kind,
                bound_to=digest,
                approver=approver,
                approved_at=now,
                decision="approved" if approved else "denied",
                snapshot=p.snapshot if kind == "patch" else None,
            )
            for kind, digest in p.requirements.items()
        ]
        self.store.approve(records)
        return records

    def _check_approval(self, p: PreparedRun) -> None:
        self._check_meta(p)
        records = {r.kind: r for r in self.store.approvals(p.plan.run_id)}
        for kind, digest in p.requirements.items():
            record = records.get(kind)
            if (
                record is None
                or record.project != p.plan.project
                or record.bound_to != digest
                or record.decision != "approved"
            ):
                raise DdakToolError(ErrorCode.APPROVAL_REQUIRED, "해당 내용의 승인이 필요하다")
            if kind == "patch" and record.snapshot != p.snapshot:
                raise DdakToolError(ErrorCode.APPROVAL_REQUIRED, "패치 승인 스냅샷이 다르다")

    def _check_meta(self, p: PreparedRun) -> None:
        check_infra_summary(p.infra_summary_json, p.requirements.get("infra"))
        try:
            saved = json.loads(
                (run_dir(self.root / "runs", p.plan.run_id) / "approval-meta.json").read_text()
            )
        except (OSError, ValueError):
            raise DdakToolError(
                ErrorCode.APPROVAL_REQUIRED, "승인 메타 기록을 확인할 수 없다"
            ) from None
        if digest_json(saved) != digest_json(
            {
                "patch_meta": json.loads(p.patch_meta_json),
                "infra_summary": json.loads(p.infra_summary_json),
            }
        ):
            raise DdakToolError(ErrorCode.APPROVAL_REQUIRED, "승인 메타 기록이 변경됐다")
        directory = run_dir(self.root / "runs", p.plan.run_id)
        try:
            saved_plan = Plan.model_validate_json((directory / "plan.json").read_text())
            patch_path = directory / "approved.patch"
            saved_patch = patch_path.read_bytes() if patch_path.exists() else None
            if plan_digest(saved_plan) != p.requirements["deploy"] or saved_patch != p.patch:
                raise ValueError("changed")
        except (OSError, ValueError):
            raise DdakToolError(
                ErrorCode.APPROVAL_REQUIRED, "승인 계획·패치 기록이 변경됐다"
            ) from None

    def subscribe(self, run_id: str, subscriber: Any) -> Callable[[], None]:
        self.store.run(run_id)
        return self._buses.setdefault(run_id, EventBus()).subscribe(subscriber)

    def events(self, run_id: str, *, after: int = -1) -> list[dict[str, Any]]:
        path = run_dir(self.root / "runs", run_id) / "events.jsonl"
        if not path.exists():
            return []
        return [
            event
            for line in path.read_text().splitlines(keepends=True)
            if line.endswith("\n") and (event := json.loads(line))["seq"] > after
        ]

    def start(self, run_id: str) -> asyncio.Task[RunResult]:
        if run_id in self._tasks:
            raise DdakToolError(ErrorCode.LOCK_HELD, "동일 run은 한 번만 실행한다")
        p = self._load_prepared(run_id)
        self._check_approval(p)
        previous = {
            target: row["current"]
            for target, row in self.store.environments(p.plan.project).items()
            if row["current"] and row["status"] != "NEEDS_HUMAN"
        }
        supplied = p.context.release_artifacts
        carried_images(
            p.plan, previous, p.context.adapter_mode, supplied.images if supplied else ()
        )
        if any(previous.get(t) != old for t, old in p.context.previous_release.items()):
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "승인 뒤 이월 이미지 기준이 변경됐다"
            )
        if p.context.adapter_mode is AdapterMode.REAL and p.context.mode is RunMode.UPDATE:
            environments = self.store.environments(p.plan.project)
            for target, section in (("local", p.plan.deploy.local), ("cloud", p.plan.deploy.cloud)):
                if not section.steps:
                    continue
                previous = environments.get(target, {}).get("current")
                if not previous or previous.get("source_mode") != AdapterMode.REAL.value:
                    raise DdakToolError(
                        ErrorCode.PRECONDITION_FAILED,
                        "실제 초기 배포의 성공 기록이 필요하다. 가짜 기록으로 복구할 수 없다",
                    )
        token = self.store.acquire(p.plan.project, run_id)
        self._tokens[run_id] = token
        task = asyncio.create_task(self._execute(p, token), name=f"deploy:{run_id}")
        self._tasks[run_id] = task
        return task

    async def wait(self, run_id: str) -> RunResult:
        # 브라우저 요청 취소가 배포 작업을 취소하지 않도록 분리한다.
        return await asyncio.shield(self._tasks[run_id])

    async def _execute(self, p: PreparedRun, token: str) -> RunResult:
        self._entered.add(p.plan.run_id)
        directory = run_dir(self.root / "runs", p.plan.run_id)
        envs = self.store.environments(p.plan.project)
        previous = {
            target: row["current"]
            for target, row in envs.items()
            if row["current"] and row["status"] != "NEEDS_HUMAN"
        }
        ctx = replace(p.context, lock_token=token, previous_release=previous)
        database_deploy = any(
            s.tool == "deploy_tier" and s.tier == "db" for s in p.plan.deploy.local.steps
        )
        supplied = p.context.release_artifacts
        carried = carried_images(
            p.plan,
            p.context.previous_release,
            p.context.adapter_mode,
            supplied.images if supplied else (),
        )
        carried_observations: dict[str, dict[str, Any]] = {}
        # 이전 형식의 장부도 provider에는 동일한 검증된 경로로 전달한다.
        ctx = replace(
            ctx,
            previous_release={
                target: {
                    **old,
                    "image_sources": {
                        **(old.get("image_sources") or {}),
                        **{
                            tier: carried_image_source(old, tier, target).model_dump(mode="json")
                            for tier in carried.get(target, {})
                        },
                    },
                }
                if target in carried
                else old
                for target, old in previous.items()
            },
        )
        if ctx.release_artifacts:
            ctx = replace(
                ctx, release_artifacts=ctx.release_artifacts.model_copy(update={"observations": {}})
            )
        bus = self._buses[p.plan.run_id]
        checks: dict[Target, set[str]] = {Target.LOCAL: set(), Target.CLOUD: set()}
        last_seq = -1

        async def persist(event: RunEvent) -> None:
            with (directory / "events.jsonl").open("a") as out:
                out.write(
                    json.dumps(redact_obj(event.model_dump(mode="json")), ensure_ascii=False) + "\n"
                )

        unsubscribe = bus.subscribe(persist, critical=True)
        execution_bus = EventBus()

        async def forward(event: RunEvent) -> None:
            nonlocal last_seq
            last_seq = event.seq
            if event.type is EventType.RUN_STATE and event.status in {s.value for s in RunStatus}:
                event = event.model_copy(update={"status": "FINALIZING"})
            await bus.publish(event)

        execution_bus.subscribe(forward, critical=True)

        async def terminal(status: RunStatus) -> None:
            try:
                await bus.publish(
                    RunEvent(
                        run_id=ctx.run_id,
                        seq=last_seq + 1,
                        type=EventType.RUN_STATE,
                        ts=datetime.now(UTC).isoformat(timespec="milliseconds"),
                        status=status.value,
                    )
                )
            finally:
                unsubscribe()

        heartbeat_failed = False
        heartbeat_stop = asyncio.Event()
        owner_task = asyncio.current_task()

        async def heartbeat() -> None:
            nonlocal heartbeat_failed
            failures = 0
            while True:
                try:
                    await asyncio.wait_for(heartbeat_stop.wait(), _HEARTBEAT_INTERVAL_S)
                    return
                except TimeoutError:
                    pass
                try:
                    await asyncio.to_thread(self.store.heartbeat, ctx.project, ctx.run_id, token)
                    failures = 0
                except Exception as exc:
                    failures += 1
                    if (
                        isinstance(exc, sqlite3.OperationalError)
                        and any(word in str(exc).lower() for word in ("locked", "busy"))
                        and failures < 3
                        and not heartbeat_stop.is_set()
                    ):
                        continue
                    heartbeat_failed = True
                    if owner_task is not None and not heartbeat_stop.is_set():
                        owner_task.cancel()
                    return

        def check_lock() -> None:
            if heartbeat_failed:
                raise DdakToolError(ErrorCode.LOCK_INVALID, "잠금 갱신 실패; 상태 확인 필요")
            self.store.check_lock(ctx.project, ctx.run_id, token)

        async def before(step: PlanStep, current: RunContext) -> None:
            del step
            check_lock()
            self._check_approval(p)
            saved = Plan.model_validate_json((directory / "plan.json").read_text())
            if plan_digest(saved) != p.plan.plan_hash or plan_digest(p.plan) != p.plan.plan_hash:
                raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "실행 중 계획이 변경됐다")
            if (
                current.build_source
                and digest_json(file_manifest(Path(current.build_source)))
                != p.snapshot.build_snapshot_hash
            ):
                raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "빌드 사본이 승인 뒤 변경됐다")

        def tool_context(step: PlanStep, target: Target | None, current: RunContext) -> RunContext:
            # 전역 state.ctx에는 새 빌드만 둔다. 병렬 환경의 서로 다른 이전 digest를 섞지 않는다.
            if (
                target is Target.CLOUD
                and database_deploy
                and not any(
                    s.tier == "db" and s.tool == "deploy_tier" for s in p.plan.deploy.cloud.steps
                )
            ):
                current = replace(
                    current, images={t: ref for t, ref in current.images.items() if t != "db"}
                )
            if target is None or target.value not in carried:
                return current
            return replace(current, images={**current.images, **carried[target.value]})

        async def after(step: PlanStep, output: dict[str, Any], current: RunContext) -> RunContext:
            updated = current
            target = step.target
            section = None
            for name in (Target.LOCAL, Target.CLOUD):
                candidate = p.plan.deploy.local if name is Target.LOCAL else p.plan.deploy.cloud
                if any(s.id == step.id for s in candidate.steps):
                    target, section = name, candidate
            if output.get("release_artifacts") is not None:
                artifacts = ReleaseArtifacts.model_validate(output["release_artifacts"])
                if artifacts.snapshot != p.snapshot:
                    raise DdakToolError(
                        ErrorCode.PRECONDITION_FAILED, "빌드 결과의 스냅샷이 다르다"
                    )
                artifacts = artifacts.model_copy(update={"observations": {}})
                if database_deploy and supplied and "db" in supplied.images:
                    if "db" in artifacts.images and artifacts.images["db"] != supplied.images["db"]:
                        raise DdakToolError(
                            ErrorCode.PRECONDITION_FAILED, "빌드가 승인된 공식 DB 이미지를 변경했다"
                        )
                    artifacts = artifacts.model_copy(
                        update={"images": {**artifacts.images, "db": supplied.images["db"]}}
                    )
                updated = replace(
                    current,
                    release_artifacts=artifacts,
                    images={tier: a.ref for tier, a in artifacts.images.items()},
                )
            if output.get("observation") is not None and target and step.tier:
                artifacts = updated.release_artifacts
                if step.tier in carried.get(target.value, {}):
                    old = previous[target.value]
                    image = carried_image_source(old, step.tier, target.value).artifact
                    observed = ImageObservation.model_validate(output["observation"])
                    if image.platform_digests[observed.platform] != observed.platform_digest:
                        raise DdakToolError(
                            ErrorCode.PRECONDITION_FAILED, "이월 이미지의 플랫폼 digest가 다르다"
                        )
                    carried_observations.setdefault(target.value, {})[step.tier] = (
                        observed.model_dump(mode="json")
                    )
                elif artifacts is None:
                    raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "빌드 산출물 없이 관측했다")
                else:
                    data = artifacts.model_dump(mode="json")
                    data["observations"].setdefault(target.value, {})[step.tier] = output[
                        "observation"
                    ]
                    updated = replace(
                        updated, release_artifacts=ReleaseArtifacts.model_validate(data)
                    )
            if step.tool == "apply_infra" and self.refresh is None:
                raise DdakToolError(ErrorCode.INFRA_MISSING, "C1 환경 출력 갱신 연결이 필요하다")
            if self.refresh:
                updated = self.refresh(step, output, updated)
                if (
                    updated.run_id != current.run_id
                    or updated.project != current.project
                    or updated.lock_token != current.lock_token
                    or updated.mode != current.mode
                    or updated.targets != current.targets
                    or updated.trigger != current.trigger
                    or updated.project_settings != current.project_settings
                    or updated.cloud_domain != current.cloud_domain
                    or updated.source_sha != current.source_sha
                    or updated.candidate_sha != current.candidate_sha
                    or updated.build_source != current.build_source
                    or updated.source_binding != current.source_binding
                ):
                    raise DdakToolError(
                        ErrorCode.PRECONDITION_FAILED, "환경 갱신이 실행 식별자를 바꿨다"
                    )
            if (
                updated.release_artifacts
                and updated.release_artifacts.snapshot != updated.source_binding
            ):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "승인 source_binding과 빌드 산출물이 다르다"
                )
            platform_outputs(updated)
            if any(set(updated.images) & set(tiers) for tiers in carried.values()):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "미빌드 tier 이미지의 전역 덮어쓰기 거부"
                )
            if target:
                if self.registry.spec(step.tool).effect is Effect.STATE_CHANGE:
                    checks[target].clear()
                if (
                    step.tool in {"health_check", "smoke_test", "verify_tls"}
                    and output.get("passed") is True
                ):
                    checks[target].add(step.tool)
            opens_verified = step.signal in {"local_verified", "cloud_verified"} or (
                section is not None
                and section.steps[-1].id == step.id
                and section.signal in {"local_verified", "cloud_verified"}
            )
            if opens_verified and current.adapter_mode is AdapterMode.REAL and target:
                required_checks = {"health_check", "smoke_test"}
                if target is Target.CLOUD:
                    required_checks.add("verify_tls")
                if not required_checks.issubset(checks[target]):
                    raise DdakToolError(
                        ErrorCode.PRECONDITION_FAILED, "배포 후 필수 기능 검사가 아직 끝나지 않았다"
                    )
                artifacts = updated.release_artifacts
                observed = artifacts.observations.get(target.value, {}) if artifacts else {}
                expected = set(tool_context(step, target, updated).images)
                if not expected or expected - (
                    set(observed) | set(carried_observations.get(target.value, {}))
                ):
                    raise DdakToolError(
                        ErrorCode.PRECONDITION_FAILED, "필수 tier 이미지 관측이 없다"
                    )
            write_context(self.root / "runs", ctx.run_id, updated.to_json_dict())
            return updated

        def rollback_tier(step: PlanStep) -> str | None:
            if step.tool == "prepare_db":
                return step.tier or "was"
            return step.tier if step.tool == "deploy_tier" else None

        planned_rollback_tiers = {
            target: list(dict.fromkeys(tier for s in section.steps if (tier := rollback_tier(s))))
            for target, section in (
                (Target.LOCAL, p.plan.deploy.local),
                (Target.CLOUD, p.plan.deploy.cloud),
            )
        }

        rollback_tiers: dict[Target, list[str]] = {Target.LOCAL: [], Target.CLOUD: []}

        def invoked(step: PlanStep, current: RunContext) -> None:
            target = step.target
            for name in (Target.LOCAL, Target.CLOUD):
                section = p.plan.deploy.local if name is Target.LOCAL else p.plan.deploy.cloud
                if any(s.id == step.id for s in section.steps):
                    target = name
            if target and step.tool in {"deploy_tier", "prepare_db"}:
                tier = rollback_tier(step)
                if tier and tier not in rollback_tiers[target]:
                    rollback_tiers[target].append(tier)

        async def rollback(target: Target, current: RunContext) -> None:
            registered = self.registry.get("rollback_tier")
            for tier in reversed(rollback_tiers[target]):
                check_lock()
                inp = registered.input_model.model_validate(
                    {"run_id": ctx.run_id, "target": target, "tier": tier, "lock_token": token}
                )
                deadline = time.monotonic() + registered.spec.timeout_s
                if current.deadline is not None:
                    deadline = min(deadline, current.deadline)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "롤백 제한 시간 초과")
                bounded = replace(current, deadline=deadline)
                if registered.is_async:
                    output = await asyncio.wait_for(registered.fn(inp, bounded), remaining)
                else:
                    output = await asyncio.wait_for(
                        asyncio.to_thread(registered.fn, inp, bounded),
                        remaining + 2,
                    )
                if (
                    not isinstance(output, registered.output_model)
                    or getattr(output, "passed", True) is False
                ):
                    raise DdakToolError(ErrorCode.ADAPTER_FAILED, "롤백 결과가 성공하지 않았다")

        heart = asyncio.create_task(heartbeat())
        result: RunResult
        build_files: dict[str, dict[str, Any]] = {}
        git_record: dict[str, Any] = {"status": "NOT_CONFIGURED"}
        merge_conflicts: list[str] = []
        candidate_stop = threading.Event()
        run_repository = None
        git_timing_start = 0
        try:
            if any(previous.get(t) != old for t, old in p.context.previous_release.items()):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "승인 뒤 이월 이미지 기준이 변경됐다"
                )
            try:
                run_repository = await asyncio.to_thread(self.connect_repository, ctx)
                if (
                    ctx.source_sha
                    and ctx.adapter_mode is AdapterMode.REAL
                    and run_repository is None
                ):
                    raise DdakToolError(
                        ErrorCode.CONFIG_INVALID, "Git 실행에 앱 저장소 연결이 필요하다"
                    )
            except Exception as exc:
                git_record = {
                    "status": "FAILED",
                    "phase": "repository",
                    "code": exc.code.value
                    if isinstance(exc, DdakToolError)
                    else ErrorCode.CONFIG_INVALID.value,
                    "detail": "앱 저장소 연결 또는 승인 origin 검사 실패",
                }
                raise
            git_timing_start = len(run_repository.timings) if run_repository else 0
            rollback_timeouts = {
                target: len(tiers) * (self.registry.spec("rollback_tier").timeout_s + 2) + 2
                for target, tiers in planned_rollback_tiers.items()
                if tiers
            }
            if (
                digest_json(p.context.to_json_dict()) != p.context_hash
                or p.facts_reader(p.source) != p.plan.facts_hash
            ):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "승인 뒤 환경 또는 facts가 변경됐다"
                )
            materialize(p.source, directory / "build-source", p.snapshot, p.patch)
            build_files = file_manifest(directory / "build-source")
            ctx = replace(
                ctx, build_source=str(directory / "build-source"), source_binding=p.snapshot
            )
            repository = run_repository
            if ctx.source_sha and repository:
                if ctx.adapter_mode is AdapterMode.FAKE and not repository.allow_local:
                    raise DdakToolError(
                        ErrorCode.CONFIG_INVALID, "FAKE 실행은 실제 앱 Git을 쓰지 않는다"
                    )

                def candidate_guard() -> None:
                    if candidate_stop.is_set():
                        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "후보 생성 취소 요청")
                    check_lock()
                    self._check_approval(p)
                    saved_plan = Plan.model_validate_json((directory / "plan.json").read_text())
                    if (
                        plan_digest(saved_plan) != p.plan.plan_hash
                        or plan_digest(p.plan) != p.plan.plan_hash
                        or digest_json(p.context.to_json_dict()) != p.context_hash
                        or p.facts_reader(p.source) != p.plan.facts_hash
                        or digest_json(file_manifest(directory / "build-source"))
                        != p.snapshot.build_snapshot_hash
                    ):
                        raise DdakToolError(
                            ErrorCode.PRECONDITION_FAILED, "후보 생성 중 승인 내용이 바뀌었다"
                        )

                if ctx.candidate_sha:
                    await self._repository_work(
                        repository.validate_candidate,
                        ctx.source_sha,
                        ctx.candidate_sha,
                        p.source_files,
                        build_files,
                        directory / "candidate-check",
                        candidate_guard,
                        cancel_event=candidate_stop,
                    )
                else:
                    candidate = await self._repository_work(
                        repository.prepare_candidate,
                        ctx.source_sha,
                        p.source_files,
                        build_files,
                        p.patch,
                        directory / "candidate",
                        candidate_guard,
                        directory / "build-source",
                        complete_on_cancel=True,
                        cancel_event=candidate_stop,
                    )
                    merge_conflicts = candidate["merge_conflicts"]
                    ctx = replace(ctx, candidate_sha=candidate["candidate_sha"])
                    (directory / "candidate.json").write_text(json.dumps(candidate))
                if candidate_stop.is_set():
                    raise asyncio.CancelledError
            elif ctx.source_sha and ctx.adapter_mode is AdapterMode.REAL:
                raise DdakToolError(
                    ErrorCode.CONFIG_INVALID, "Git 실행에 앱 저장소 연결이 필요하다"
                )
            if ctx.release_artifacts and ctx.release_artifacts.snapshot != p.snapshot:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "입력 이미지의 소스 결합이 다르다"
                )
            write_context(self.root / "runs", ctx.run_id, ctx.to_json_dict())
            result = await Executor(
                self.registry,
                bus=execution_bus,
                rollback=rollback,
                before_step=before,
                after_step=after,
                tool_context=tool_context,
                on_invoke=invoked,
                rollback_timeouts=rollback_timeouts,
            ).run(p.plan, ctx)
            repository = run_repository
            final_context = result.context or ctx
            if (
                repository
                and final_context.candidate_sha
                and (ctx.adapter_mode is AdapterMode.REAL or repository.allow_local)
            ):
                selected = {
                    name for name in ("local", "cloud") if getattr(p.plan.deploy, name).steps
                }
                succeeded = {
                    name for name in selected if result.tracks.get(name) is TrackStatus.DONE
                }
                try:
                    self.store.check_lock(ctx.project, ctx.run_id, token)
                    publish = repository.publish
                    if result.status is RunStatus.FAILED_VERIFY:
                        # 환경별 배포 태그는 남기되 비교 불가를 main 승격 성공으로 숨기지 않는다.
                        publish = partial(repository.publish, update_main=False)
                    git_record = await self._repository_work(
                        publish,
                        final_context.candidate_sha,
                        selected,
                        succeeded,
                        complete_on_cancel=True,
                    )
                except (Exception, asyncio.CancelledError) as publication_error:
                    # 배포는 이미 끝났다. 기록 실패 때문에 실제 배포 관측을 버리지 않는다.
                    git_record = {"status": "FAILED", "error": type(publication_error).__name__}

        except (Exception, asyncio.CancelledError) as exc:
            # 엔진 진입 전 실패는 대상 무변경. 엔진이 반환하지 못한 예외는 상태를 보수적으로 막는다.
            unknown = (directory / "events.jsonl").exists()
            status = (
                RunStatus.NEEDS_HUMAN
                if unknown
                else RunStatus.CANCELLED
                if candidate_stop.is_set()
                else RunStatus.FAILED_BEFORE_DEPLOY
            )
            result = RunResult(ctx.run_id, status, {}, [])
            (directory / "error.json").write_text(
                json.dumps(
                    {"type": type(exc).__name__, "detail": "실행 시작/기록 실패; 상태 확인 필요"}
                )
            )
        finally:
            heartbeat_stop.set()
            # to_thread 취소는 SQLite 작업 종료가 아니다. 진행 중 갱신 결과를 봉인 전에 확인한다.
            try:
                await asyncio.shield(heart)
            except asyncio.CancelledError:
                heartbeat_failed = True
                await asyncio.shield(heart)
        if run_repository:
            with contextlib.suppress(OSError):
                (directory / "git-timings.json").write_text(
                    json.dumps(run_repository.timings[git_timing_start:], indent=2)
                )
        if heartbeat_failed:
            result = replace(result, status=RunStatus.NEEDS_HUMAN)
        final_ctx = result.context or ctx
        final_data = {
            "status": result.status.value,
            "git": git_record,
            "merge_conflicts": merge_conflicts,
            "infra_changes": result.infra_changes,
            "targets": final_ctx.targets,
            "trigger": final_ctx.trigger,
            "tracks": {k: v.value for k, v in result.tracks.items()},
            "steps": {
                r.step_id: {
                    "status": r.status,
                    "elapsed_s": r.elapsed_s,
                    "output": r.output,
                    "error": r.error,
                }
                for r in result.records
            },
        }
        release = {
            "release_id": ctx.run_id,
            "targets": final_ctx.targets,
            "trigger": final_ctx.trigger,
            "source_sha": final_ctx.source_sha,
            "candidate_sha": final_ctx.candidate_sha,
            "git": git_record,
            "merge_conflicts": merge_conflicts,
            "infra_changes": result.infra_changes,
            "source_mode": p.context.adapter_mode.value,
            "platform_outputs": platform_outputs(final_ctx),
            "source": p.snapshot.model_dump(mode="json"),
            "files": build_files,
            "source_files": p.source_files,
            "images": dict(final_ctx.images),
            "artifacts": final_ctx.release_artifacts.model_dump(mode="json")
            if final_ctx.release_artifacts
            else None,
        }
        changes: dict[str, tuple[str, dict[str, Any] | None]] = {}
        for target in ("local", "cloud"):
            section = p.plan.deploy.local if target == "local" else p.plan.deploy.cloud
            if not section.steps:
                continue
            track = result.tracks.get(target)
            if track is TrackStatus.DONE:
                target_release = release
                if (
                    target == "cloud"
                    and database_deploy
                    and not any(
                        s.tier == "db" and s.tool == "deploy_tier"
                        for s in p.plan.deploy.cloud.steps
                    )
                ):
                    target_artifacts = release["artifacts"]
                    if target_artifacts:
                        target_artifacts = {
                            **target_artifacts,
                            "images": {
                                t: a for t, a in target_artifacts["images"].items() if t != "db"
                            },
                            "observations": {
                                env: {t: o for t, o in items.items() if t != "db"}
                                for env, items in target_artifacts["observations"].items()
                            },
                        }
                    target_release = {
                        **release,
                        "images": {t: ref for t, ref in release["images"].items() if t != "db"},
                        "artifacts": target_artifacts,
                    }
                current_release = environment_release(
                    target_release, previous.get(target, {}), target
                )
                if target == "local" and database_deploy:
                    current_release["image_sources"]["db"]["source"] = "approved_mysql_artifact"
                for tier, observation in carried_observations.get(target, {}).items():
                    current_release["image_sources"][tier]["observation"] = observation
                changes[target] = (
                    "SUCCEEDED",
                    current_release,
                )
            elif track is TrackStatus.ROLLED_BACK:
                changes[target] = ("ROLLED_BACK", None)
            elif result.status is RunStatus.NEEDS_HUMAN:
                changes[target] = ("NEEDS_HUMAN", None)
        manifest = {
            "schema": "ddak.release/v1",
            **release,
            "result": final_data,
            "source_mode": p.context.adapter_mode.value,
            "sealed_at": datetime.now(UTC).isoformat(),
            "environment_images": {
                target: {"images": entry[1]["images"], "image_sources": entry[1]["image_sources"]}
                for target, entry in changes.items()
                if entry[1] is not None
            },
        }
        # SQLite 봉인이 기준이다. 파일 export 실패로 성공한 배포를 실패로 뒤집지 않는다.
        try:
            self.store.finish(ctx.run_id, result.status.value, final_data, manifest, changes)
        except Exception:
            result = replace(result, status=RunStatus.NEEDS_HUMAN)
            # DB 전체 장애면 상태 갱신도 실패할 수 있다. 기존 잠금은 반드시 남긴다.
            with contextlib.suppress(Exception):
                self.store.mark_stopped(ctx.run_id, "NEEDS_HUMAN")
            with contextlib.suppress(Exception):
                await terminal(RunStatus.NEEDS_HUMAN)
            return result
        with contextlib.suppress(OSError):
            temporary = directory / "release.json.tmp"
            temporary.write_text(
                json.dumps(release_view(manifest), ensure_ascii=False, indent=2) + "\n"
            )
            temporary.replace(directory / "release.json")
        try:
            await terminal(result.status)
        except Exception:
            result = replace(result, status=RunStatus.NEEDS_HUMAN)
            with contextlib.suppress(Exception):
                self.store.mark_stopped(ctx.run_id, "NEEDS_HUMAN")
        if result.status is not RunStatus.NEEDS_HUMAN:
            self.store.release(ctx.project, ctx.run_id, token)
        return result
