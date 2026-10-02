"""조립 진입점: 툴 모듈을 레지스트리에 채우고 관리 웹을 만든다. 모든 툴 모듈을 import하는 유일한 곳.

- 개발·데모: 호스트에서 `make run`(= uv run python -m ddak). 127.0.0.1에만 바인드(✅ 장부 11).
  로그인해 둔 Claude CLI, 호스트 docker, ~/.aws 프로필을 그대로 쓴다.
- 배포·공유용 컨테이너 이미지 1개(docker run 한 줄)는 TODO(O1).
  💭 컨테이너에서는 웹(관리 화면·계획)과 실행기(배포 툴, docker·AWS 권한)를 두 컨테이너로
  나누고, docker.sock 직접 마운트 대신 socket proxy 또는 DOCKER_HOST=ssh://를 쓰는 안을 검토한다.
- ddak.core.ai는 LLM 상태 함수를 웹에 주입하려고만 import한다(호출하지 않는다).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import os
import shutil
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI

from ddak.cd import configure_cloud_tls
from ddak.cloud.infra import has_infra_binding
from ddak.cloud.tls import ensure_tls
from ddak.core.ai.status import llm_status
from ddak.core.app_repository import AppRepository
from ddak.core.config import AdapterMode, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Plan
from ddak.core.contracts.plan_facts import FileMeta
from ddak.core.logging import get_logger
from ddak.core.project_settings import ProjectSettings
from ddak.core.registry import REGISTRY, Registry, import_tools
from ddak.core.snapshots import copy_source
from ddak.executor.approval_meta import check_infra_summary, encode_meta
from ddak.executor.infra import refresh_infra_context
from ddak.executor.service import DeploymentService
from ddak.onprem.inventory import load_inventory
from ddak.plan import new_run_id, plan_deployment
from ddak.plan.intake import FetchPolicy, Watcher, WatchTarget, load_watch_targets, resolve_head
from ddak.web.app import create_app

_log = get_logger("plan")

# 이 패키지들 바로 아래 <디렉토리>/tool.py를 자동 탐색한다(tool.py가 없는 디렉토리는 건너뜀).
TOOL_PACKAGES = (
    "ddak.core.tools",
    "ddak.plan",  # plan/<intake|detect|analyze|planner|validate|patch|dockerfile>/tool.py
    "ddak.cloud.infra.tools",
    "ddak.cloud.build.tools",
    "ddak.cloud",  # cloud/<deploy|tls|health>/tool.py (verify_tls는 cloud/health)
    "ddak.cd.tools",
    "ddak.verify",  # verify/<smoke|compare|diagnose|report>/tool.py
    "ddak.ops.tools",
)


def load_tools() -> Registry:
    """TOOL_PACKAGES를 자동 탐색해 등록한다. 여러 번 불러도 같은 결과다(모듈 캐시)."""
    configure_cloud_tls(ensure_tls)
    for name in TOOL_PACKAGES:
        import_tools(importlib.import_module(name))
    return REGISTRY


def _previous_manifests(store: Any, project: str) -> dict[Any, dict[str, FileMeta] | None]:
    """환경별 마지막 성공 배포의 원본 파일 해시(source_files). 성공 기록이 없으면 None."""
    envs = store.environments(project)
    out: dict[Any, dict[str, FileMeta] | None] = {}
    for env in ("local", "cloud"):
        files = ((envs.get(env) or {}).get("current") or {}).get("source_files")
        out[env] = None if files is None else {k: FileMeta(**v) for k, v in files.items()}
    return out


def _platform_bootstrap_plan(plan: Plan, ctx: RunContext, summary: dict | None) -> Plan:
    """승인 전에 첫 플랫폼의 선행 의존성을 조립한다. 실행 중 계획을 우회하지 않는다."""
    if ctx.mode is not RunMode.BOOTSTRAP or not summary or summary["layer"] != "platform":
        return plan
    prepared = plan.model_copy(deep=True, update={"plan_hash": None})
    infra = [s for s in prepared.deploy.cloud.steps if s.tool == "apply_infra"]
    if len(infra) != 1 or infra[0].wait_for:
        raise DdakToolError(
            ErrorCode.PLAN_INVALID, "첫 플랫폼 적용은 선행 인프라 단계 하나가 필요하다"
        )
    first = infra[0].model_copy(update={"signal": "infra_ready"})
    prepared.deploy.cloud.steps[:] = [
        first,
        *(s for s in prepared.deploy.cloud.steps if s.tool != "apply_infra"),
    ]
    prepared.build.steps[:] = [
        s.model_copy(update={"wait_for": list(dict.fromkeys([*s.wait_for, "infra_ready"]))})
        for s in prepared.build.steps
    ]
    return prepared


async def _infra_approval(service: DeploymentService, plan: Plan, ctx: RunContext):
    if ctx.targets == "onprem" or not any(s.tool == "apply_infra" for s in plan.deploy.cloud.steps):
        return {}, None
    if not has_infra_binding(ctx.run_id):
        raise DdakToolError(
            ErrorCode.INFRA_MISSING, "generate_infra 번들 및 run별 인프라 세션 연결이 없다"
        )
    # 이미 조립된 번들만 검증한다. 생성기 입출력 계약을 임의로 추정하거나 가짜 plan을 만들지 않는다.
    output = {}
    for name in ("validate_infra", "plan_infra"):
        tool = service.registry.get(name)
        inp = tool.input_model.model_validate({"run_id": ctx.run_id})
        bounded = replace(ctx, deadline=time.monotonic() + tool.spec.timeout_s)
        if tool.is_async:
            result = await asyncio.wait_for(tool.fn(inp, bounded), tool.spec.timeout_s)
        else:
            # 동기 실행기의 자식 프로세스 제한 시간은 C1 runtime이 관리한다.
            result = await asyncio.to_thread(tool.fn, inp, bounded)
        if not isinstance(result, tool.output_model) or getattr(result, "passed", None) is not True:
            raise DdakToolError(ErrorCode.INFRA_MISSING, f"{name} 검사 불합격")
        output = result.model_dump(mode="json")
    digest = output["plan_sha256"]
    summary = output["summary"]
    check_infra_summary(encode_meta(summary, infra=True), digest)
    return {"infra": digest}, summary


def _watch_targets(service: DeploymentService) -> list[WatchTarget]:
    saved = {s["project"]: s for s in service.list_project_settings()}
    targets = [
        WatchTarget(
            project,
            s["repo_url"],
            s.get("watch_branch", "prod"),
            "local" if s.get("default_targets") == "onprem" else s.get("default_targets", "both"),
        )
        for project, s in saved.items()
        if s.get("auto_detect") and s.get("repo_url")
    ]
    if os.environ.get("DDAK_WATCH_REPO_URL"):
        targets += [replace(t, ref="prod") for t in load_watch_targets() if t.project not in saved]
    return targets


def _attach_watch(app: FastAPI, settings: Settings) -> None:
    """O2 감시 구현은 유지하고 조립부에서 저장 설정의 대상/브랜치를 공급한다."""
    policy = FetchPolicy.from_env()
    inner = app.router.lifespan_context

    async def on_new_commit(t: WatchTarget, sha: str) -> None:
        service = app.state.deployment
        rid = await _prepare_commit(service, settings, t, sha, policy=policy)
        if service.get_run(rid)["status"] == "FAILED_BEFORE_DEPLOY":
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, f"배포 준비 실패: {rid}")

    @asynccontextmanager
    async def lifespan(a: FastAPI) -> AsyncIterator[Any]:
        async with inner(a) as state:
            stopped = asyncio.Event()

            async def supervise():
                current = []
                watcher = None
                task = None

                async def stop_watcher():
                    if watcher is not None:
                        await watcher.stop()
                    if task is not None:
                        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                            await asyncio.wait_for(task, 5)

                try:
                    while not stopped.is_set():
                        targets = _watch_targets(a.state.deployment)
                        if targets != current:
                            await stop_watcher()
                            current = targets
                            watcher = Watcher(targets, on_new_commit, policy=policy)
                            task = asyncio.create_task(watcher.run(), name="repo-watch")
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(stopped.wait(), 1)
                finally:
                    await stop_watcher()

            supervisor = asyncio.create_task(supervise(), name="repo-watch-settings")
            try:
                yield state
            finally:
                stopped.set()
                await supervisor

    app.router.lifespan_context = lifespan


async def _prepare_commit(
    service: DeploymentService,
    settings: Settings,
    target: WatchTarget,
    sha: str | None,
    *,
    policy: FetchPolicy,
    trigger: Literal["auto", "manual"] = "auto",
) -> str:
    """감지한 SHA를 계획·승인에 연결한다. 실행은 승인 이후에만 가능하다."""
    run_id = new_run_id()
    context = None
    plan = None
    owned_source = None
    phase = "request"
    try:
        saved = service.get_project_settings(target.project) or {}
        if saved.get("repo_url") and saved["repo_url"] != target.repo_url:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "감시 저장소와 프로젝트 설정이 다르다"
            )
        if trigger == "auto" and saved.get("watch_branch") and saved["watch_branch"] != target.ref:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "감시 브랜치와 프로젝트 설정이 다르다"
            )
        previous = _previous_manifests(service.store, target.project)
        selected = ("local", "cloud") if target.target == "both" else (target.target,)
        existing = [previous[t] is not None for t in selected]
        if any(existing) and not all(existing):
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED,
                "대상별 초기 배포 상태가 다르다. 아직 배포되지 않은 대상만 선택해 초기 배포한다",
            )
        mode = RunMode.UPDATE if all(existing) else RunMode.BOOTSTRAP
        request = DeployRequest(
            project=target.project,
            repo_url=target.repo_url,
            ref=target.ref,
            target=target.target,
            mode=mode,
        )
        context = RunContext(
            run_id=run_id,
            project=request.project,
            mode=mode,
            adapter_mode=settings.adapter_mode,
            repo_url=request.repo_url,
            ref=request.ref,
            source_sha=sha,
            trigger=trigger,
            cloud_domain=saved.get("cloud_domain"),
            targets="onprem" if request.target == "local" else request.target,
            project_settings={
                k: v
                for k, v in saved.items()
                if k in ProjectSettings.model_fields or k == "version"
            },
        )
        if sha is None:
            phase = "resolve"
            ref = request.ref
            if ref and not ref.startswith("refs/tags/"):
                ref = "refs/heads/" + ref
            if ref and ref.startswith("refs/tags/"):
                repository = await asyncio.to_thread(service.connect_repository, context)
                if repository is None:
                    raise DdakToolError(
                        ErrorCode.CONFIG_INVALID, "태그 조회에 앱 저장소 연결이 필요하다"
                    )
                sha = await asyncio.to_thread(repository.resolve_tag, ref)
            else:
                sha = await asyncio.to_thread(resolve_head, request.repo_url, ref, policy=policy)
            context = replace(context, source_sha=sha)
        platform: dict[str, Any] = {}
        phase = "inventory"
        path = os.environ.get("DDAK_ONPREM_INVENTORY")
        if request.target != "cloud" and path:
            platform["onprem"] = load_inventory(Path(path))
        context = replace(context, platform=platform)
        # 브랜치가 다음 poll 전에 움직여도 감지된 커밋만 intake한다.
        phase = "plan"
        bundle = await asyncio.to_thread(
            plan_deployment,
            request.model_copy(update={"ref": sha}),
            run_id=run_id,
            settings=settings,
            fetch_policy=policy,
            previous_manifests=lambda project: _previous_manifests(service.store, project),
            cloud_domain=context.cloud_domain,
            platform=platform,
        )
        plan = bundle.plan
        _log.info("새 커밋 계획 생성", run_id=run_id, project=target.project, commit=sha)
        latest = service.get_project_settings(target.project) or {}
        phase = "settings"
        if latest.get("version") != saved.get("version"):
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "계획 중 프로젝트 설정이 변경됐다")
        context = replace(
            bundle.context,
            repo_url=context.repo_url,
            ref=context.ref,
            source_sha=sha,
            trigger=trigger,
            targets=context.targets,
            platform=platform,
            project_settings=context.project_settings,
            cloud_domain=context.cloud_domain,
        )
        if service.repository_factory is not None:
            phase = "repository"
            await asyncio.to_thread(service.connect_repository, context)
        phase = "infra"
        subjects, infra_summary = await _infra_approval(service, bundle.plan, context)
        # intake 캐시는 TTL로 정리된다. 승인 대기 소스는 컨트롤러 수명과 분리해 보관한다.
        source = service.root / "sources" / run_id
        phase = "source"
        if not source.exists() and not source.is_symlink():
            owned_source = source
        # asyncio 취소는 복사 스레드를 멈추지 않는다. 종료를 확인한 뒤 실패 사본을 정리한다.
        copying = asyncio.create_task(asyncio.to_thread(copy_source, bundle.source, source))
        try:
            await asyncio.shield(copying)
        except asyncio.CancelledError:
            while not copying.done():
                try:
                    await asyncio.shield(copying)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not copying.cancelled():
                copying.exception()
            raise
        phase = "prepare"
        plan = _platform_bootstrap_plan(bundle.plan, context, infra_summary)
        service.prepare(
            plan,
            context,
            source,
            subjects=subjects,
            infra_summary=infra_summary,
            expected_settings_version=saved.get("version", 0),
        )
    except (Exception, asyncio.CancelledError) as exc:
        failure = (
            DdakToolError(ErrorCode.PRECONDITION_FAILED, "배포 준비 요청이 취소됐다")
            if isinstance(exc, asyncio.CancelledError)
            else exc
        )
        recorded = service.record_preparation_failure(
            run_id, target.project, failure, context=context, phase=phase, plan=plan
        )
        # 이번 요청이 만든 실패 사본만 정리한다. 기존 경로/승인·실행 중 run은 보존한다.
        if (
            recorded
            and owned_source is not None
            and owned_source.is_dir()
            and not owned_source.is_symlink()
        ):
            try:
                await asyncio.to_thread(shutil.rmtree, owned_source)
            except OSError:
                _log.warning("실패 소스 사본 정리 실패", run_id=run_id)
        _log.warning("배포 준비 실패: 실행 기록 확인", run_id=run_id, error_type=type(exc).__name__)
        if isinstance(exc, asyncio.CancelledError):
            raise
    else:
        _log.info("새 커밋 승인 대기", run_id=run_id, project=target.project, commit=sha)
    return run_id


def _manual_planning(settings: Settings, policy: FetchPolicy):
    async def prepare(service: DeploymentService, request: DeployRequest) -> str:
        target = WatchTarget(request.project, request.repo_url, request.ref, request.target)
        return await _prepare_commit(
            service, settings, target, None, policy=policy, trigger="manual"
        )

    return prepare


def _repository_factory(root: Path, *, allow_local: bool = False):
    lock = threading.Lock()

    def connect(ctx: RunContext) -> AppRepository:
        if not ctx.repo_url:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "앱 저장소 URL이 필요하다")
        if ctx.adapter_mode is AdapterMode.FAKE and (
            not allow_local or not ctx.repo_url.startswith("file://")
        ):
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "FAKE 앱 checkout에는 로컬 저장소 fixture가 필요하다"
            )
        identity = hashlib.sha256(ctx.repo_url.encode()).hexdigest()
        with lock:
            return AppRepository.connect(
                root / ctx.project / identity, ctx.repo_url, allow_local=allow_local
            )

    return connect


def create() -> FastAPI:
    registry = load_tools()
    settings = Settings.from_env()
    app = create_app(
        llm_status=llm_status,
        deployment_factory=lambda: DeploymentService(
            registry,
            settings.run_dir.parent,
            refresh=refresh_infra_context,
            planning_flow=_manual_planning(settings, FetchPolicy.from_env()),
            repository_factory=_repository_factory(settings.run_dir.parent / "repositories"),
        ),
        settings=settings,
    )
    _attach_watch(app, settings)
    return app


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    # 외부에 열지 않는다(127.0.0.1). 온프렘 앱 기본 주소 8080과 겹치지 않게 8765.
    # 포트는 💭(설계 문서 00 N21, 하네스 I-26).
    uvicorn.run(create(), host="127.0.0.1", port=settings.admin_port)
