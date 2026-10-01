"""C3/O2 진입점: prepare → approval_view → approve → start/wait.

웹 인증/CSRF는 C3 책임이다. 이 객체는 신뢰된 컨트롤러 내부 API이며 HTTP에 그대로 노출하지 않는다.
팀 툴은 레지스트리로만 호출한다. 사실 재계산은 O2가 주입하는 facts_reader를 사용한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from ddak.core.config import AdapterMode
from ddak.core.contracts.approval import ApprovalRecord
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Effect, Layer, RunMode, Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.events import EventType, RunEvent
from ddak.core.contracts.plan import Plan, PlanStep
from ddak.core.contracts.release import ReleaseArtifacts, SnapshotBinding
from ddak.core.redact import redact_obj
from ddak.core.registry import Registry
from ddak.core.runlog import run_dir, write_context
from ddak.core.snapshots import digest_json, file_manifest, materialize, preview
from ddak.core.store import Store, release_view
from ddak.executor.approval_meta import check_infra_summary, encode_meta
from ddak.executor.engine import Executor, RunResult, RunStatus, TrackStatus, check_signals
from ddak.executor.events import EventBus

ApprovalKind = Literal["patch", "deploy", "infra", "dockerfile", "foundation"]
FactsReader = Callable[[Path], str]
ContextRefresh = Callable[[PlanStep, dict[str, Any], RunContext], RunContext]
_HEARTBEAT_INTERVAL_S = 10.0


def plan_digest(plan: Plan) -> str:
    return digest_json(plan.model_dump(mode="json", by_alias=True, exclude={"plan_hash"}))


def source_facts(root: Path) -> str:
    return digest_json(file_manifest(root))


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
        self, registry: Registry, root: Path, *, refresh: ContextRefresh | None = None
    ) -> None:
        self.registry, self.root, self.refresh = registry, root, refresh
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
        self._prepared: dict[str, PreparedRun] = {}
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
                project = self._prepared[run_id].plan.project
                self.store.mark_stopped(run_id, "CANCELLED")
                self.store.release(project, run_id, self._tokens[run_id])
            else:
                task.cancel()
        if pending:
            await asyncio.gather(*pending.values(), return_exceptions=True)
        self.close()

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
    ) -> str:
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
        snapshot = preview(source, patch)
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
        self._prepared[plan.run_id] = PreparedRun(
            plan,
            context,
            source.resolve(),
            snapshot,
            patch,
            required,
            facts_reader,
            digest_json(context.to_json_dict()),
            file_manifest(source),
            patch_meta_json,
            infra_summary_json,
        )
        self._buses[plan.run_id] = EventBus()
        return plan.run_id

    def approval_view(self, run_id: str) -> dict[str, Any]:
        p = self._prepared[run_id]
        return {
            "run_id": run_id,
            "project": p.plan.project,
            "subjects": dict(p.requirements),
            "snapshot": p.snapshot.model_dump(mode="json"),
            "patch": p.patch.decode() if p.patch else None,
            "plan": p.plan.model_dump(mode="json", by_alias=True),
            "patch_meta": json.loads(p.patch_meta_json),
            "infra_summary": json.loads(p.infra_summary_json),
        }

    def approve(self, run_id: str, *, approver: str, approved: bool = True) -> list[ApprovalRecord]:
        p = self._prepared[run_id]
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

    def subscribe(self, run_id: str, subscriber: Any) -> Callable[[], None]:
        return self._buses[run_id].subscribe(subscriber)

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
        p = self._prepared[run_id]
        self._check_approval(p)
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
        previous = {target: row["current"] for target, row in envs.items() if row["current"]}
        ctx = replace(p.context, lock_token=token, previous_release=previous)
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
                updated = replace(
                    current,
                    release_artifacts=artifacts,
                    images={tier: a.ref for tier, a in artifacts.images.items()},
                )
            if output.get("observation") is not None and target and step.tier:
                artifacts = updated.release_artifacts
                if artifacts is None:
                    raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "빌드 산출물 없이 관측했다")
                data = artifacts.model_dump(mode="json")
                data["observations"].setdefault(target.value, {})[step.tier] = output["observation"]
                updated = replace(updated, release_artifacts=ReleaseArtifacts.model_validate(data))
            if step.tool == "apply_infra" and self.refresh is None:
                raise DdakToolError(ErrorCode.INFRA_MISSING, "C1 환경 출력 갱신 연결이 필요하다")
            if self.refresh:
                updated = self.refresh(step, output, updated)
                if (
                    updated.run_id != current.run_id
                    or updated.project != current.project
                    or updated.lock_token != current.lock_token
                    or updated.mode != current.mode
                    or updated.build_source != current.build_source
                ):
                    raise DdakToolError(
                        ErrorCode.PRECONDITION_FAILED, "환경 갱신이 실행 식별자를 바꿨다"
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
                if not updated.images or set(updated.images) - set(observed):
                    raise DdakToolError(
                        ErrorCode.PRECONDITION_FAILED, "필수 tier 이미지 관측이 없다"
                    )
            write_context(self.root / "runs", ctx.run_id, updated.to_json_dict())
            return updated

        rollback_tiers = {
            target: list(
                dict.fromkeys(s.tier for s in section.steps if s.tool == "deploy_tier" and s.tier)
            )
            for target, section in (
                (Target.LOCAL, p.plan.deploy.local),
                (Target.CLOUD, p.plan.deploy.cloud),
            )
        }

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
        try:
            rollback_timeouts = {
                target: len(tiers) * (self.registry.spec("rollback_tier").timeout_s + 2) + 2
                for target, tiers in rollback_tiers.items()
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
            ctx = replace(ctx, build_source=str(directory / "build-source"))
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
                rollback_timeouts=rollback_timeouts,
            ).run(p.plan, ctx)
        except (Exception, asyncio.CancelledError) as exc:
            # 엔진 진입 전 실패는 대상 무변경. 엔진이 반환하지 못한 예외는 상태를 보수적으로 막는다.
            unknown = (directory / "events.jsonl").exists()
            status = RunStatus.NEEDS_HUMAN if unknown else RunStatus.FAILED_BEFORE_DEPLOY
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
        if heartbeat_failed:
            result = replace(result, status=RunStatus.NEEDS_HUMAN)
        final_ctx = result.context or ctx
        final_data = {
            "status": result.status.value,
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
            "source_mode": p.context.adapter_mode.value,
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
            if result.status is RunStatus.NEEDS_HUMAN:
                changes[target] = ("NEEDS_HUMAN", release if track is TrackStatus.DONE else None)
            elif track is TrackStatus.DONE:
                changes[target] = ("SUCCEEDED", release)
            elif track is TrackStatus.ROLLED_BACK:
                changes[target] = ("ROLLED_BACK", None)
        if (
            result.status in {RunStatus.FAILED_CLOUD, RunStatus.PARITY_FAILED}
            and result.tracks.get("local") is TrackStatus.DONE
            and p.plan.deploy.cloud.steps
        ):
            changes["local"] = ("DIVERGED", release)
            changes["cloud"] = ("DIVERGED", None)
        manifest = {
            "schema": "ddak.release/v1",
            **release,
            "result": final_data,
            "source_mode": p.context.adapter_mode.value,
            "sealed_at": datetime.now(UTC).isoformat(),
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
