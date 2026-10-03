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
import json
import os
import secrets
import shutil
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from fastapi import FastAPI

from ddak.cd import configure_cloud_tls
from ddak.cloud.build import preflight_local_build
from ddak.cloud.deploy import seed_registry_secrets
from ddak.cloud.infra import (
    bind_infra,
    create_binding,
    fixture_binding,
    has_infra_binding,
    read_bundle,
)
from ddak.cloud.infra.storage_allocation import prepare_storage_context
from ddak.cloud.tls import ensure_tls
from ddak.core.ai.gateway import get_jev_client
from ddak.core.ai.providers import (
    provider_catalog,
    provider_status,
    test_provider_connection,
    validate_provider_selection,
)
from ddak.core.ai.status import llm_status
from ddak.core.app_repository import AppRepository, FakeAppRepository
from ddak.core.config import AdapterMode, Settings, require_local_cli
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.enums import RunMode, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.patch_review import PatchReviewRequest, ReviewResult
from ddak.core.contracts.plan import Plan, PlanStep
from ddak.core.contracts.plan_facts import FileMeta
from ddak.core.contracts.tools.generate_infra import GenerateInfraInput, GenerateInfraOutput
from ddak.core.contracts.tools.patch_config import PatchConfigInput, PatchConfigOutput
from ddak.core.defaults import (
    cloud_platform_default,
    load_aws_defaults,
    load_defaults,
    project_values,
)
from ddak.core.logging import get_logger
from ddak.core.project_settings import ProjectSettings, watch_source
from ddak.core.redact import redact_obj
from ddak.core.registry import REGISTRY, Registry, import_tools
from ddak.core.runlog import run_dir
from ddak.core.runtime import tool_context
from ddak.core.runtime_values import derived_public
from ddak.core.setup_actions import SetupActions
from ddak.core.setup_service import SetupService
from ddak.core.setup_tools import BuildSetup
from ddak.core.snapshots import copy_source, digest_json
from ddak.core.tool_paths import managed_tools
from ddak.executor.approval_meta import check_infra_summary, encode_meta
from ddak.executor.infra import refresh_infra_context
from ddak.executor.patch_review import PatchReviews
from ddak.executor.preparation import missing_track_tools
from ddak.executor.service import DeploymentService
from ddak.onprem.deploy import (
    DatabaseAccount,
    DatabasePreparationPlan,
    OnPremPreparationManager,
    preflight_inventory,
    write_inventory,
)
from ddak.onprem.inventory import load_inventory
from ddak.plan import new_run_id, plan_deployment, replan_patch
from ddak.plan.intake import (
    FetchPolicy,
    Watcher,
    WatchTarget,
    initial_trigger_from_env,
    interval_from_env,
    load_watch_targets,
    resolve_head,
)
from ddak.plan.patch import (
    PatchPreparation,
    PatchSession,
    combine_review,
    patch_session,
    proposal_diff,
)
from ddak.web.app import create_app

_log = get_logger("plan")
ADMIN_HOST = "127.0.0.1"

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
    """승인 전에 빌드의 인프라 의존성을 조립한다. 실행 중 계획을 우회하지 않는다."""
    if ctx.mode is RunMode.UPDATE:
        cloud = ctx.platform.get("cloud") or {}
        required = (
            (ctx.image_repository,)
            if ctx.build_backend == "local"
            else (cloud.get("codebuild_project_name"), cloud.get("image_repository"))
        )
        if not all(isinstance(value, str) and value.strip() for value in required):
            return plan
        if not any("infra_ready" in step.wait_for for step in plan.build.steps):
            return plan
        prepared = plan.model_copy(deep=True, update={"plan_hash": None})
        prepared.build.steps[:] = [
            step.model_copy(
                update={"wait_for": [signal for signal in step.wait_for if signal != "infra_ready"]}
            )
            for step in prepared.build.steps
        ]
        return prepared
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


def _refresh_cloud_context(step: PlanStep, output: dict[str, Any], ctx: RunContext) -> RunContext:
    """Terraform 출력을 반영하고 첫 CodeBuild 전에 레지스트리 자격증명을 채운다."""
    updated = refresh_infra_context(step, output, ctx)
    if step.tool == "apply_infra" and output.get("layer") == "platform":
        seed_registry_secrets(updated)
    return updated


async def _infra_approval(service: DeploymentService, plan: Plan, ctx: RunContext):
    if (
        ctx.targets == "onprem"
        or "cloud" in ctx.preparation_failures
        or not any(s.tool == "apply_infra" for s in plan.deploy.cloud.steps)
    ):
        return {}, None
    if not has_infra_binding(ctx.run_id) and ctx.adapter_mode is AdapterMode.FAKE:
        bind_infra(
            fixture_binding(
                ctx,
                root=service.root / "infra-fixture",
                approvals=lambda: service.store.approvals(ctx.run_id),
                guard=lambda: service.guard_infra(ctx.run_id, ctx.project),
            )
        )
    if not has_infra_binding(ctx.run_id):
        if "generate_infra" not in service.registry.registered():
            raise DdakToolError(ErrorCode.INFRA_MISSING, "generate_infra 툴이 등록되지 않았다")
        tool = service.registry.get("generate_infra")
        directory = (service.root / "infra-bundles" / ctx.run_id).resolve()
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        request = GenerateInfraInput(
            run_id=ctx.run_id,
            directory=str(directory),
            layer="platform" if ctx.mode is RunMode.BOOTSTRAP else "app",
        )
        bounded = replace(ctx, deadline=time.monotonic() + tool.spec.timeout_s)
        try:
            with tool_context("generate_infra", ctx.run_id):
                result = await asyncio.wait_for(
                    tool.fn(request, bounded)
                    if tool.is_async
                    else asyncio.to_thread(tool.fn, request, bounded),
                    tool.spec.timeout_s,
                )
        except TimeoutError:
            # 동기 생성기의 스레드를 강제 종료하지 않는다. 이 run 번들은 재사용/적용하지 않는다.
            raise DdakToolError(
                ErrorCode.ADAPTER_TIMEOUT, "generate_infra 제한 시간 초과; 생성 번들 격리"
            ) from None
        bundle = GenerateInfraOutput.model_validate(result)
        if bundle.layer != request.layer:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "생성 번들의 인프라 층이 다르다")
        files = read_bundle(bundle, directory)
        binding = await asyncio.to_thread(
            create_binding,
            bundle,
            files,
            ctx,
            root=service.root / "infra",
            approvals=lambda: service.store.approvals(ctx.run_id),
            guard=lambda: service.guard_infra(ctx.run_id, ctx.project),
        )
        bind_infra(replace(binding, generation_source=bundle.source))
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


def _watch_configuration(service: DeploymentService) -> tuple[list[WatchTarget], list[str]]:
    saved = {s["project"]: {**s, **project_values(s)} for s in service.list_project_settings()}
    targets = [
        WatchTarget(
            project,
            s["repo_url"],
            s.get("watch_branch", "prod"),
            "local"
            if s.get("default_targets", "onprem") == "onprem"
            else s.get("default_targets", "onprem"),
        )
        for project, s in saved.items()
        if s.get("auto_detect") and s.get("repo_url")
    ]
    if os.environ.get("DDAK_WATCH_REPO_URL"):
        targets += [
            replace(t, project=service.resolve_project(t.project))
            for t in load_watch_targets()
            if service.resolve_project(t.project) not in saved
        ]
    preferred = service.resolve_project(os.environ.get("DDAK_WATCH_PROJECT") or "flaskr")
    chosen: dict[tuple[str, str], WatchTarget] = {}
    warnings = []
    for target in sorted(targets, key=lambda t: (t.project != preferred, t.project)):
        try:
            identity = watch_source(target.repo_url, target.ref)
        except ValueError:
            warnings.append(f"자동 감시 설정 오류: {target.project}의 저장소 URL을 확인하세요")
            continue
        if identity in chosen:
            winner = chosen[identity]
            warnings.append(
                f"중복 자동 감시: {winner.project}만 감시하고 {target.project}는 제외했습니다. "
                "같은 저장소·브랜치의 기존 프로젝트 설정에서 자동 감지를 끄세요"
            )
        else:
            chosen[identity] = target
    return list(chosen.values()), warnings


def _watch_targets(service: DeploymentService) -> list[WatchTarget]:
    return _watch_configuration(service)[0]


def _project_fetch_policy(service, base: FetchPolicy, project: str, url: str) -> FetchPolicy:
    if service.onboarding is None:
        return base
    return replace(base, token=None, credentials=(service.onboarding.vault.path, project, url))


def _attach_watch(app: FastAPI, settings: Settings) -> None:
    """O2 감시 구현은 유지하고 조립부에서 저장 설정의 대상/브랜치를 공급한다."""
    policy = FetchPolicy.from_env()
    policy = replace(policy, root=policy.root.expanduser().resolve())
    inner = app.router.lifespan_context
    app.state.watch_warnings = lambda: _watch_configuration(app.state.deployment)[1]
    source_locks: dict[tuple[str, str], asyncio.Lock] = {}
    seen: dict[tuple[str, str], str] = {}
    callbacks: set[asyncio.Task] = set()

    async def on_new_commit(t: WatchTarget, sha: str) -> None:
        service = app.state.deployment
        identity = watch_source(t.repo_url, t.ref)
        callback = asyncio.current_task()
        if callback is not None:
            callbacks.add(callback)
        try:
            async with source_locks.setdefault(identity, asyncio.Lock()):
                # stop 직전 큐에 있던 callback도 현재 소유권을 다시 확인한다.
                if t not in _watch_targets(service):
                    return
                if seen.get(identity) == sha or service.store.has_auto_run(t.repo_url, t.ref, sha):
                    return
                rid = await _prepare_commit(service, settings, t, sha, policy=policy)
                row = service.get_run(rid)
                if row["status"] == "FAILED_BEFORE_DEPLOY":
                    code = (row.get("result") or {}).get("code", ErrorCode.INTERNAL.value)
                    raise DdakToolError(ErrorCode(code), f"배포 준비 실패: {rid}")
                seen[identity] = sha  # 실패·취소된 준비는 새 owner가 같은 SHA로 재시도한다.
        finally:
            callbacks.discard(callback)

    @asynccontextmanager
    async def lifespan(a: FastAPI) -> AsyncIterator[Any]:
        async with inner(a) as state:
            stopped = asyncio.Event()

            async def supervise():
                current = []
                previous_warnings = []
                watcher = None
                task = None

                async def stop_watcher():
                    if watcher is not None:
                        await watcher.stop()
                    if task is not None:
                        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                            await asyncio.wait_for(task, 5)
                    if callbacks:
                        # Watcher.stop은 handler를 취소만 한다. 정리 완료 뒤 새 owner를 띄운다.
                        await asyncio.gather(*tuple(callbacks), return_exceptions=True)

                try:
                    while not stopped.is_set():
                        targets, warnings = _watch_configuration(a.state.deployment)
                        if warnings != previous_warnings:
                            for warning in warnings:
                                _log.warning(warning)
                            previous_warnings = warnings
                        if targets != current:
                            await stop_watcher()
                            current = targets
                            watcher = Watcher(
                                targets,
                                on_new_commit,
                                policy=policy,
                                interval_s=interval_from_env(),
                                initial_trigger=initial_trigger_from_env(),
                                get_state=a.state.deployment.get_state,
                                policy_for=lambda t: _project_fetch_policy(
                                    a.state.deployment, policy, t.project, t.repo_url
                                ),
                            )
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


def _prepare_config_patch(service, source, facts, ctx, *, settings, source_root, selected):
    previous = {
        target: row["current"]
        for target, row in service.store.environments(ctx.project).items()
        if target in selected and row.get("current")
    }
    ctx = replace(ctx, previous_release=previous)
    inp = PatchConfigInput(run_id=ctx.run_id, source_dir=source.relative_to(source_root).as_posix())
    session = PatchSession(ctx.run_id, source_root, service.root / "runs", facts, settings)
    registered = service.registry.get("patch_config")
    with patch_session(session), tool_context("patch_config", ctx.run_id):
        output = registered.fn(inp, ctx)
    out = PatchConfigOutput.model_validate(output)
    if out.run_id != ctx.run_id:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "패치 툴 응답의 run ID가 다르다")
    return PatchPreparation.from_output(out)


async def _prepare_commit_inner(
    service: DeploymentService,
    settings: Settings,
    target: WatchTarget,
    sha: str | None,
    *,
    policy: FetchPolicy,
    trigger: Literal["auto", "manual"] = "auto",
) -> str:
    """감지한 SHA를 계획·승인에 연결한다. 실행은 승인 이후에만 가능하다."""
    policy = replace(policy, root=policy.root.expanduser().resolve())
    run_id = new_run_id()
    context = None
    plan = None
    owned_source = None
    facts = None
    phase = "request"
    try:
        await service.begin_preparation(target.project, run_id)
        saved = service.get_project_settings(target.project) or {}
        if service.onboarding is not None:
            settings = service.onboarding.effective(target.project, saved, service.onboarding.vault)
        aws_profile = (
            saved.get("aws_profile") or settings.aws_profile or load_defaults()["aws_profile"]
        )
        saved = {**saved, **project_values(saved)}
        if saved.get("cloud_platform") is None:
            # 관리 페이지 값이 없으면 기본 파일의 프로젝트별 플랫폼 이름을 스냅샷에 고정한다.
            saved["cloud_platform"] = cloud_platform_default(target.project)
        saved.update(
            generation_provider=settings.selected_provider("generation"),
            generation_model=settings.llm_model,
            judgment_provider=settings.selected_provider("judgment"),
            judgment_model=settings.judgment_model,
            llm_effort=settings.llm_effort,
            ai_timeout_s=settings.ai_timeout_s,
            build_backend=settings.build_backend,
            image_repository=settings.image_repository,
            aws_profile=aws_profile,
        )
        if settings.adapter_mode is AdapterMode.REAL:
            from ddak.core.git_credentials import configured_identity

            repo_path = (
                service.root
                / "repositories"
                / target.project
                / hashlib.sha256(target.repo_url.encode()).hexdigest()
            )
            name, email = configured_identity(saved, repo_path)
            saved.update(git_author_name=name, git_author_email=email)
        if saved.get("repo_url") and saved["repo_url"] != target.repo_url:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "감시 저장소와 프로젝트 설정이 다르다"
            )
        if (
            trigger == "auto"
            and saved.get("watch_branch")
            and (
                saved["watch_branch"].removeprefix("refs/heads/")
                != target.ref.removeprefix("refs/heads/")
            )
        ):
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
            code_patch=saved.get("code_patch", True),
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
            build_backend=settings.build_backend,
            image_repository=settings.image_repository
            if settings.build_backend == "local"
            else None,
            cloud_domain=saved.get("cloud_domain"),
            targets="onprem" if request.target == "local" else request.target,
            project_settings={
                "aws_expected_account_id": settings.aws_expected_account_id
                or load_aws_defaults()["expected_account_id"],
                **{
                    k: v
                    for k, v in saved.items()
                    if k in ProjectSettings.model_fields or k == "version"
                },
            },
        )
        if settings.adapter_mode is AdapterMode.REAL:
            from ddak.core.git_credentials import credential_source

            context = replace(
                context,
                project_settings={
                    **context.project_settings,
                    "git_auth_source": credential_source(
                        service.root / "private", target.project, target.repo_url
                    ),
                },
            )
        if service.onboarding is not None:
            service.onboarding.require_ready(target.project, context.targets)
            policy = _project_fetch_policy(service, policy, target.project, request.repo_url)
        if sha is None:
            phase = "resolve"
            url = urlsplit(request.repo_url)
            if (
                url.scheme not in policy.allowed_schemes
                or url.username
                or url.password
                or url.query
                or url.fragment
                or any(c.isspace() for c in request.repo_url)
                or (policy.allowed_hosts is not None and url.hostname not in policy.allowed_hosts)
            ):
                raise DdakToolError(ErrorCode.CONFIG_INVALID, "접수 정책에서 허용하지 않은 저장소")
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
        if service.onboarding is not None:
            build_context = service.onboarding.build_context(target.project)
            platform["controller_tools"] = {"tool_dir": build_context["tool_dir"]}
            if settings.build_backend == "local":
                platform["local_build"] = build_context
        phase = "inventory"
        cloud_outputs = service.get_platform_outputs(target.project, settings.adapter_mode)
        if cloud_outputs or request.target != "local":
            # region은 Terraform 출력·사용자 입력이 아니라 제품의 서울 고정 규약이다.
            platform["cloud"] = {**cloud_outputs, "region": "ap-northeast-2"}
        path = saved.get("inventory_path") or os.environ.get("DDAK_ONPREM_INVENTORY")
        if request.target != "cloud" and path:
            platform["onprem"] = load_inventory(Path(path))
            if service.onboarding is not None and saved.get("inventory_path") == path:
                was = platform["onprem"].get("tiers", {}).get("was")
                if was is not None:
                    was["env_file"] = str(service.onboarding.runtime_env(target.project))
        context = replace(context, platform=platform)
        # 브랜치가 다음 poll 전에 움직여도 감지된 커밋만 intake한다.
        phase = "plan"
        planning = asyncio.create_task(
            asyncio.to_thread(
                plan_deployment,
                request.model_copy(update={"ref": sha}),
                run_id=run_id,
                settings=settings,
                fetch_policy=policy,
                **(
                    {"jev_client": get_jev_client(settings)}
                    if service.onboarding is not None
                    else {}
                ),
                previous_manifests=lambda project: _previous_manifests(service.store, project),
                patch_preparer=lambda source, facts, ctx: _prepare_config_patch(
                    service,
                    source,
                    facts,
                    ctx,
                    settings=settings,
                    source_root=policy.root,
                    selected=selected,
                ),
                record_stage=lambda name, ms, status: service.record_stage(
                    run_id, name, ms, status
                ),
                cloud_domain=context.cloud_domain,
                platform=platform,
                source_context=context,
            )
        )
        try:
            bundle = await asyncio.shield(planning)
        except asyncio.CancelledError:
            # coroutine 취소는 계획 스레드를 멈추지 않는다. 다음 감시 소유자를 시작하기
            # 전에 종료를 확인하며, 반복 취소도 기존 작업의 종료 대기를 끊지 못한다.
            while not planning.done():
                try:
                    await asyncio.shield(planning)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not planning.cancelled():
                planning.exception()
            raise
        plan = bundle.plan
        if getattr(bundle, "facts", None) is not None:
            facts = bundle.facts.model_dump(mode="json")
            # Facts에는 키 이름/종류만 있다. 자유 서술 reason은 값 유출을 막기 위해 보관하지 않는다.
            for key in facts.get("env_keys", []):
                key["reason"] = None
            facts = redact_obj(facts)
        _log.info("새 커밋 계획 생성", run_id=run_id, project=target.project, commit=sha)
        latest = service.get_project_settings(target.project) or {}
        phase = "settings"
        if latest.get("version") != saved.get("version"):
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "계획 중 프로젝트 설정이 변경됐다")
        phase = "prepare"
        context = replace(
            bundle.context,
            repo_url=context.repo_url,
            ref=context.ref,
            source_sha=sha,
            trigger=trigger,
            targets=context.targets,
            platform=platform,
            project_settings={
                **context.project_settings,
                **{
                    key: bundle.context.project_settings[key]
                    for key in ("_infra_storage", "_infra_mapping_suggestions")
                    if key in bundle.context.project_settings
                },
            },
            cloud_domain=context.cloud_domain,
            build_backend=context.build_backend,
            image_repository=context.image_repository,
            preparation_failures=missing_track_tools(plan, service.registry),
            required_env_keys=tuple(k.name for k in bundle.facts.env_keys if k.required)
            if getattr(bundle, "facts", None) is not None
            else (),
        )
        if "onprem" in platform and "was" in platform["onprem"].get("tiers", {}):
            was = platform["onprem"]["tiers"]["was"]
            was["public_env"] = {
                **derived_public(platform["onprem"], context.required_env_keys),
                **was.get("public_env", {}),
            }
        if service.repository_factory is not None:
            phase = "source_preflight"
            context = await _source_preflight(
                service, context, patch=getattr(bundle, "patch", None)
            )
        phase = "infra"
        try:
            context = await asyncio.to_thread(prepare_storage_context, context, service.root)
            subjects, infra_summary = await _infra_approval(service, bundle.plan, context)
        except DdakToolError as exc:
            if request.target != "both" or not bundle.plan.deploy.local.steps:
                raise
            context = replace(
                context,
                preparation_failures={**context.preparation_failures, "cloud": ["apply_infra"]},
                preparation_errors={
                    "cloud": {
                        "phase": "infra",
                        "code": exc.code.value,
                        "detail": str(redact_obj(exc.message))[:1000],
                    }
                },
            )
            subjects, infra_summary = {}, None
        # intake 캐시는 TTL로 정리된다. 승인 대기 소스는 컨트롤러 수명과 분리해 보관한다.
        source = service.root / "sources" / run_id
        phase = "source"
        if not source.exists() and not source.is_symlink():
            owned_source = source
        with service.preparation_stage(run_id, "source"):
            # asyncio 취소는 복사 스레드를 멈추지 않는다. 종료를 확인한 뒤 실패 사본을 정리한다.
            if bundle.source.expanduser().is_symlink():
                raise DdakToolError(ErrorCode.CONFIG_INVALID, "소스 심볼릭 링크는 지원하지 않는다")
            bundle_source = bundle.source.expanduser().resolve()
            copying = asyncio.create_task(asyncio.to_thread(copy_source, bundle_source, source))
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
            patch=getattr(bundle, "patch", None),
            patch_meta=getattr(bundle, "patch_meta", None),
            patch_review=getattr(bundle, "patch_review", None),
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
    finally:
        service.end_preparation(target.project, run_id)
        if facts is not None:
            try:
                directory = run_dir(service.root / "runs", run_id)
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / "facts.json"
                path.touch(mode=0o600, exist_ok=True)
                path.write_text(json.dumps(facts, ensure_ascii=False, indent=2) + "\n")
            except OSError:
                _log.warning("facts 내보내기 실패", run_id=run_id)
    return run_id


async def _prepare_commit(service, settings, target, sha, *, policy, trigger="auto"):
    directory = None
    if service.onboarding is not None:
        directory = service.onboarding.build_context(target.project).get("tool_dir")
    with managed_tools(directory):
        return await _prepare_commit_inner(
            service, settings, target, sha, policy=policy, trigger=trigger
        )


def _manual_planning(settings: Settings, policy: FetchPolicy):
    policy = replace(policy, root=policy.root.expanduser().resolve())

    async def prepare(service: DeploymentService, request: DeployRequest) -> str:
        target = WatchTarget(request.project, request.repo_url, request.ref, request.target)
        return await _prepare_commit(
            service, settings, target, None, policy=policy, trigger="manual"
        )

    return prepare


def _repository_factory(root: Path, *, allow_local: bool = False, vault_root: Path | None = None):
    root = root.expanduser().resolve()
    lock = threading.Lock()

    def connect(ctx: RunContext) -> AppRepository:
        if not ctx.repo_url:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "앱 저장소 URL이 필요하다")
        identity = hashlib.sha256(ctx.repo_url.encode()).hexdigest()
        with lock:
            if ctx.adapter_mode is AdapterMode.FAKE and not (
                allow_local and ctx.repo_url.startswith("file://")
            ):
                path = root / "fake" / ctx.project / identity
                path.mkdir(parents=True, exist_ok=True)
                return FakeAppRepository(path, allow_local=True, expected_url=ctx.repo_url)
            options = {}
            if ctx.adapter_mode is AdapterMode.REAL and not allow_local:
                from ddak.core.git_credentials import configured_identity

                author = configured_identity(ctx.project_settings, root / ctx.project / identity)
                if vault_root is None:
                    raise DdakToolError(ErrorCode.CONFIG_INVALID, "앱 저장소 인증 연결 필요")
                options = {"author": author, "credentials": (vault_root, ctx.project, ctx.repo_url)}
            return AppRepository.connect(
                root / ctx.project / identity, ctx.repo_url, allow_local=allow_local, **options
            )

    return connect


def _local_build_preflight(ctx: RunContext) -> list[str]:
    if ctx.adapter_mode is AdapterMode.REAL:
        return preflight_local_build(
            ctx.image_repository or "", config=ctx.platform.get("local_build")
        )
    return []


async def _source_preflight(
    service: DeploymentService, context: RunContext, *, patch: bytes | None = None
) -> RunContext:
    repository = await asyncio.to_thread(service.connect_repository, context)
    if repository is None or not context.source_sha:
        if context.adapter_mode is AdapterMode.REAL:
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "승인 전 소스 검사에 Git 연결과 SHA가 필요하다"
            )
        return context
    summary = (
        {"source": "fixture", "ignored_count": 0, "findings": []}
        if isinstance(repository, FakeAppRepository)
        else await asyncio.to_thread(repository.preflight_source, context.source_sha, patch=patch)
    )
    return replace(context, source_checks=summary)


def _configure_onprem(service: DeploymentService) -> None:
    project = service.resolve_project(os.environ.get("DDAK_WATCH_PROJECT") or "flaskr")
    saved = service.get_project_settings(project) or {}
    if saved.get("repo_url"):
        return  # 저장된 사람이 정한 설정은 프로필이 덮어쓰지 않는다.
    repo = os.environ.get("DDAK_WATCH_REPO_URL")
    if repo:
        defaults = load_defaults()
        branch = os.environ.get("DDAK_WATCH_BRANCH") or defaults["watch_branch"]
        identity = watch_source(repo, branch)
        for s in service.list_project_settings():
            if s["project"] == project or not s.get("auto_detect") or not s.get("repo_url"):
                continue
            try:
                other_source = watch_source(s["repo_url"], s.get("watch_branch", "prod"))
            except ValueError:
                continue  # 기존 잘못된 URL은 감시 조립에서 경고하고 제외한다.
            if other_source == identity:
                # 환경변수 후보는 감시 조립에서 선택·경고한다. 중복 설정은 만들지 않는다.
                return
        service.save_project_settings(
            project,
            {
                "repo_url": repo,
                "watch_branch": branch,
                "default_targets": defaults["default_targets"],
                "auto_detect": defaults["auto_detect"],
            },
            updated_by="local-profile",
            expected_version=saved.get("version", 0),
        )


def _effective_project_settings(base, project, saved, vault, *, cli_host, check_local=True):
    updates = {}
    sources = dict(base.setting_sources)
    defaults = load_defaults() if base.adapter_mode is AdapterMode.REAL else {}
    fallback = Settings()
    fields = {
        "generation_provider": ("llm_provider", "DDAK_LLM_PROVIDER"),
        "generation_model": ("llm_model", "DDAK_LLM_MODEL"),
        "judgment_provider": ("judgment_provider", "DDAK_JUDGMENT_PROVIDER"),
        "judgment_model": ("judgment_model", "DDAK_JUDGMENT_MODEL"),
        "llm_effort": ("llm_effort", "DDAK_LLM_EFFORT"),
        "ai_timeout_s": ("ai_timeout_s", "DDAK_AI_TIMEOUT_S"),
        "build_backend": ("build_backend", "DDAK_BUILD_BACKEND"),
        "image_repository": ("image_repository", "DDAK_IMAGE_REPOSITORY"),
        "aws_profile": ("aws_profile", "DDAK_AWS_PROFILE"),
    }
    for stored, (field, variable) in fields.items():
        legacy = {
            "generation_provider": "DDAK_LLM_BACKEND",
            "judgment_provider": "DDAK_JEV_BACKEND",
            "aws_profile": "AWS_PROFILE",
        }.get(stored)
        if saved.get(stored) is not None:
            updates[field] = saved[stored]
            sources[stored] = "관리 페이지"
        elif stored not in sources:
            overridden = os.environ.get(variable) or (legacy and os.environ.get(legacy))
            explicit = getattr(base, field) != getattr(fallback, field)
            if stored in defaults and not overridden and not explicit:
                updates[field] = defaults[stored]
                sources[stored] = "기본 파일"
            else:
                sources[stored] = "실행환경"
    provider_keys = dict(getattr(base, "provider_keys", {}))
    for spec in provider_catalog():
        key = spec.get("key_name")
        if key:
            value = vault.get(project, key)
            if value and not getattr(base, key, None):
                provider_keys[key] = value
                if key in {"anthropic_api_key", "groq_api_key"}:
                    updates[key] = value
    updates["provider_keys"] = provider_keys
    catalog = {p["id"]: p for p in provider_catalog()}
    for role, provider_field, model_field in (
        ("generation", "llm_provider", "llm_model"),
        ("judgment", "judgment_provider", "judgment_model"),
    ):
        provider = updates.get(provider_field) or base.selected_provider(role)
        model_key = role + "_model"
        provider_changed = provider != base.selected_provider(role)
        explicit_model = (
            sources.get(model_key) == "실행환경" and getattr(base, model_field) is not None
        )
        if provider not in catalog:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "등록되지 않은 AI provider 설정")
        if saved.get(model_key) is None and (
            (provider_changed and not explicit_model) or sources.get(model_key) == "기본 파일"
        ):
            updates[model_field] = (
                defaults.get(model_key)
                if provider == defaults.get(role + "_provider")
                else catalog[provider]["default_model"]
            ) or None
            sources[model_key] = (
                "기본 파일" if provider == defaults.get(role + "_provider") else "실행환경"
            )
    updates["setting_sources"] = sources
    if base.adapter_mode is AdapterMode.REAL and base.aws_expected_account_id is None:
        updates["aws_expected_account_id"] = load_aws_defaults()["expected_account_id"]
    result = replace(base, **updates)
    if check_local:
        require_local_cli(result, host=cli_host)
    return result


def _setup_service(service, settings, cli_host):
    def effective(project, saved, vault):
        return _effective_project_settings(
            settings, project, {**saved, "project": project}, vault, cli_host=cli_host
        )

    def display_effective(project, saved, vault):
        return _effective_project_settings(
            settings, project, saved, vault, cli_host=cli_host, check_local=False
        )

    def inventory_probe(project, saved):
        path = saved.get("inventory_path") or os.environ.get("DDAK_ONPREM_INVENTORY")
        if not path:
            return {"status": "gray", "detail": "온프레미스 환경 등록 필요"}
        result = preflight_inventory(load_inventory(Path(path)), project=project)
        return {
            "status": "green" if result["passed"] else "red",
            "detail": "VM·Docker·지문 점검 완료"
            if result["passed"]
            else "온프레미스 사전 점검 실패",
        }

    def repository_probe(project, saved):
        if not saved.get("repo_url"):
            return {"status": "gray", "detail": "앱 저장소 URL 필요"}
        if settings.adapter_mode is AdapterMode.REAL:
            from ddak.core.git_credentials import configured_identity

            try:
                repo_path = (
                    service.root
                    / "repositories"
                    / project
                    / hashlib.sha256(saved["repo_url"].encode()).hexdigest()
                )
                name, email = configured_identity(saved, repo_path)
                saved = {**saved, "git_author_name": name, "git_author_email": email}
            except DdakToolError as error:
                return {"status": "red", "detail": str(error)}
        context = RunContext(
            "setup-probe",
            settings.adapter_mode,
            project=project,
            repo_url=saved["repo_url"],
            project_settings=saved,
        )
        from ddak.core.git_credentials import credential_source

        label = ""
        try:
            source = credential_source(service.root / "private", project, saved["repo_url"])
            label = "관리 페이지 토큰" if source == "managed" else "머신 Git 자격 증명"
            repository = service.connect_repository(context)
            if repository is None or isinstance(repository, FakeAppRepository):
                return {"status": "gray", "detail": "fixture 저장소; 실제 push 권한 미확인"}
            repository.check_push_access(saved.get("watch_branch", "prod"))
        except DdakToolError as error:
            return {"status": "red", "detail": (label + " · " if label else "") + str(error)}
        return {"status": "green", "detail": label + " · 일반 push dry-run 통과; 원격 변경 없음"}

    def test_connection(provider_id, cfg, *, role=None):
        spec = next(p for p in provider_catalog() if p["id"] == provider_id)
        if spec["kind"] == "cli":
            require_local_cli(replace(cfg, llm_provider=provider_id), host=cli_host)
        return test_provider_connection(provider_id, cfg, role=role)

    def status(provider_id, cfg):
        spec = next(p for p in provider_catalog() if p["id"] == provider_id)
        if spec["kind"] == "cli":
            require_local_cli(replace(cfg, llm_provider=provider_id), host=cli_host)
        return provider_status(provider_id, cfg)

    return SetupService(
        service,
        catalog=provider_catalog,
        effective=effective,
        display_effective=display_effective,
        test_provider=test_connection,
        validate_selection=validate_provider_selection,
        inventory_writer=write_inventory,
        inventory_reader=load_inventory,
        build_factory=BuildSetup,
        provider_status=status,
        probes={"inventory": inventory_probe, "repository": repository_probe},
    )


def _setup_actions(service, settings):
    """준비 UI에는 공개 계획만, 비밀번호는 승인 뒤 제품 Vault/전용 env로 연결한다."""

    class Adapter:
        def __init__(self, project):
            self.project = project
            self.setup = service.onboarding
            saved = service.get_project_settings(project) or {}
            path = saved.get("inventory_path") or os.environ.get("DDAK_ONPREM_INVENTORY")
            if settings.adapter_mode is not AdapterMode.REAL or not path:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "REAL 온프레미스 인벤토리 등록이 필요하다"
                )
            self.inventory = load_inventory(Path(path))
            self.saved = saved
            self.replica = 1
            previous = service.store.environments(project).get("local", {}).get("current")
            ctx = RunContext(
                new_run_id(),
                settings.adapter_mode,
                project=project,
                mode=RunMode.UPDATE if previous else RunMode.BOOTSTRAP,
                platform={"onprem": self.inventory},
            )
            self.manager = OnPremPreparationManager(
                service.root,
                ctx,
                password_reader=lambda ref: self.setup.vault.get(project, ref),
            )

        def plan(self, kind, args):
            if kind == "ownership":
                self.replica = args["replica"]
                return self.manager.plan_ownership(args["tier"], replica=self.replica)
            # WAS가 접근할 DB 주소만 사용한다. SSH 포트는 DB 서비스 포트가 아니다.
            db = self.inventory.get("tiers", {}).get("db", {})
            was = self.inventory.get("tiers", {}).get("was", {})
            if not db or not was:
                raise DdakToolError(ErrorCode.CONFIG_INVALID, "DB와 WAS tier 등록 필요")
            host = was.get("public_env", {}).get("DB_HOST")
            if not host:
                host = (db.get("ssh") or self.inventory.get("ssh") or {}).get("host")
            if not host and self.inventory.get("mode") == "container":
                host = db.get("name")
            if not host or any(c in host for c in "/@?#\r\n"):
                raise DdakToolError(ErrorCode.CONFIG_INVALID, "DB 접근 호스트 필요")
            port = was.get("public_env", {}).get("DB_PORT", "3306")
            if not str(port).isdigit() or not 1 <= int(port) <= 65535:
                raise DdakToolError(ErrorCode.CONFIG_INVALID, "DB 접근 포트 형식 오류")
            self.endpoint = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            accounts = tuple(
                DatabaseAccount(
                    args[key],
                    role,
                    "DB_PASSWORD_" + hashlib.sha256(args[key].encode()).hexdigest()[:24].upper(),
                )
                for key, role in (("app_account", "app"), ("migrator_account", "migrator"))
            )
            plan = self.manager.plan_database(
                database=args["database"],
                backup_database=args["backup_database"],
                accounts=accounts,
            )
            if any(
                a.name in plan.observed.accounts
                and not self.setup.vault.configured(self.project, a.password_env_ref)
                for a in accounts
            ):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED,
                    "기존 계정 암호는 알 수 없다. 새 계정 이름을 쓰거나 기존 DB URL을 등록하세요",
                )
            return plan

        def apply(self, plan, approvalcheck):
            current = service.get_project_settings(self.project) or {}
            path = current.get("inventory_path") or os.environ.get("DDAK_ONPREM_INVENTORY")
            if not path or load_inventory(Path(path)) != self.inventory:
                raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "인벤토리 변경; 재계획 필요")
            if not isinstance(plan, DatabasePreparationPlan):
                self.manager.apply_ownership(
                    plan,
                    approved_sha=plan.approval_sha,
                    approval_check=approvalcheck,
                    replica=self.replica,
                )
                return
            if not approvalcheck(plan.approval_sha, plan.summary):
                raise DdakToolError(ErrorCode.APPROVAL_REQUIRED, "DB 준비 승인 필요")
            for account in plan.accounts:
                if not self.setup.vault.configured(self.project, account.password_env_ref):
                    self.setup.vault.put(
                        self.project, account.password_env_ref, secrets.token_hex(32)
                    )
            self.manager.apply_database(
                plan, approved_sha=plan.approval_sha, approval_check=approvalcheck
            )
            try:
                urls = {}
                for account in plan.accounts:
                    password = self.setup.vault.get(self.project, account.password_env_ref)
                    urls[account.role] = (
                        "mysql+pymysql://"
                        + quote(account.name, safe="")
                        + ":"
                        + quote(password, safe="")
                        + "@"
                        + self.endpoint
                        + "/"
                        + plan.database
                    )
                self.setup.save_env(self.project, "DATABASE_URL", urls["app"])
                self.setup.save_migration_url(self.project, urls["migrator"])
            except Exception:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED,
                    "DB 준비 완료 뒤 연결 값 저장 실패; DB·앱 설정 확인 필요",
                    needs_human=True,
                ) from None

    return SetupActions(service, Adapter)


def _previous_built_tiers(store, project: str) -> dict:
    """이월된 이미지도 실제 빌드 당시 파일로 비교한다. 원본 manifest로 패치를 지우지 않는다."""
    result = {}
    for target, state in store.environments(project).items():
        current = state.get("current")
        if current is None:
            continue
        tiers = {}
        for tier in current.get("images", {}):
            origin = current.get("image_sources", {}).get(tier, {})
            if origin.get("source") == "approved_mysql_artifact":
                continue
            origin_id = origin.get("release_id", current.get("release_id"))
            record = (
                current
                if origin_id == current.get("release_id")
                else store.release_record(origin_id)
            )
            files = record.get("files") if record else None
            if not isinstance(files, dict):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "이전 이미지의 빌드 기록을 확인할 수 없습니다"
                )
            tiers[tier] = {name: FileMeta(**value) for name, value in files.items()}
        result[target] = tiers
    return result


def _review_tool_output(service, settings, source, context, review):
    """검토도 같은 등록 툴·프로젝트 설정·성공 원장으로 검사한다."""
    saved = service.get_project_settings(context.project) or {}
    if service.onboarding is not None:
        settings = service.onboarding.effective(context.project, saved, service.onboarding.vault)
    selected = (
        ("local",)
        if context.targets == "onprem"
        else (("cloud",) if context.targets == "cloud" else ("local", "cloud"))
    )
    context = replace(
        context,
        previous_release={
            target: row["current"]
            for target, row in service.store.environments(context.project).items()
            if target in selected and row.get("current")
        },
        toggles={**context.toggles, "code_patch": True},
    )
    session = PatchSession(context.run_id, source.parent, service.root / "runs", None, settings)
    request = PatchConfigInput(run_id=context.run_id, source_dir=source.name, review=review)
    registered = service.registry.get("patch_config")
    with patch_session(session), tool_context("patch_config", context.run_id):
        output = PatchConfigOutput.model_validate(registered.fn(request, context))
    if output.run_id != context.run_id:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "패치 툴 응답의 run ID가 다릅니다")
    prepared = PatchPreparation.from_output(output)
    if output.status == "rejected":
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED,
            "; ".join(output.warnings) or "패치 툴 검사를 통과하지 못했습니다",
        )
    return output, prepared


def _configure_patch_review(service: DeploymentService, settings: Settings) -> None:
    def generate(source, context, *, previous=None, allproposals=None, prompt=""):
        request = PatchReviewRequest(
            action="revise" if previous else "propose",
            proposals=allproposals or [],
            proposal_id=previous.id if previous else "",
            prompt=prompt,
        )
        output, checked = _review_tool_output(service, settings, source, context, request)
        proposals = output.proposals
        if previous is not None:
            proposals = [item for item in proposals if item.id == previous.id]
        elif combine_review(source, proposals, [item.id for item in proposals]) != checked.patch:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "제안과 검사된 패치가 다릅니다")
        origin = output.source or (output.meta.source if output.meta else Source.LIVE)
        return ReviewResult(proposals, origin, checked.warnings)

    async def finalize(prepared, patch: bytes | None, draft: dict, guard) -> str:
        guard()
        original = prepared.context
        run_id = new_run_id()
        source = service.root / "sources" / run_id
        context = replace(
            original,
            run_id=run_id,
            source_binding=None,
            candidate_sha=None,
            release_artifacts=None,
            images={},
            preparation_errors={},
            preparation_failures={},
            preparation_warnings=[],
            source_checks={},
            review_baseline_hash=None,
            toggles={**original.toggles, "code_patch": bool(patch)},
        )
        if not original.repo_url:
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "재계획에는 소스 저장소 기록이 필요합니다"
            )
        baseline = digest_json(service.store.environments(original.project))
        saved = service.get_project_settings(original.project) or {}
        effective = settings
        if service.onboarding is not None:
            effective = service.onboarding.effective(
                original.project, saved, service.onboarding.vault
            )
        try:
            await service._repository_work(copy_source, prepared.source, source)
            _, checked = await service._repository_work(
                partial(
                    _review_tool_output,
                    service,
                    effective,
                    source,
                    context,
                    PatchReviewRequest(
                        action="compose", proposals=draft["proposals"], selected=draft["selected"]
                    ),
                )
            )
            if checked.patch != patch:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "선택한 수정과 검사 결과가 다릅니다"
                )
            request = DeployRequest(
                project=original.project,
                repo_url=original.repo_url,
                ref=original.source_sha,
                target="local" if original.targets == "onprem" else (original.targets or "both"),
                mode=original.mode,
                code_patch=bool(checked.patch),
            )
            bundle = await service._repository_work(
                partial(
                    replan_patch,
                    request,
                    run_id=run_id,
                    source=source,
                    patch=checked.patch,
                    settings=effective,
                    source_context=context,
                    previous_manifests=lambda project: _previous_manifests(service.store, project),
                    previous_tier_manifests=lambda project: _previous_built_tiers(
                        service.store, project
                    ),
                    cloud_domain=context.cloud_domain,
                    platform=dict(context.platform),
                    patch_env_keys=checked.env_keys,
                    record_stage=lambda name, ms, status: service.record_stage(
                        run_id, name, ms, status
                    ),
                    **(
                        {"jev_client": get_jev_client(effective)}
                        if service.onboarding is not None
                        else {}
                    ),
                )
            )
            context = replace(
                bundle.context,
                review_baseline_hash=baseline,
                preparation_warnings=list(
                    dict.fromkeys([*draft.get("warnings", []), *checked.warnings])
                ),
                required_env_keys=tuple(key.name for key in bundle.facts.env_keys if key.required),
                preparation_failures=missing_track_tools(bundle.plan, service.registry),
            )
            # 신규 필수 키에도 현재 main의 온프렘 파생 설정을 제공한다.
            platform = json.loads(json.dumps(context.platform))
            if "was" in platform.get("onprem", {}).get("tiers", {}):
                was = platform["onprem"]["tiers"]["was"]
                was["public_env"] = {
                    **derived_public(platform["onprem"], context.required_env_keys),
                    **was.get("public_env", {}),
                }
            context = replace(context, platform=platform)
            if service.repository_factory is not None:
                context = await _source_preflight(service, context, patch=checked.patch)
            try:
                context = await asyncio.to_thread(prepare_storage_context, context, service.root)
                subjects, infra_summary = await _infra_approval(service, bundle.plan, context)
            except DdakToolError as exc:
                if request.target != "both" or not bundle.plan.deploy.local.steps:
                    raise
                context = replace(
                    context,
                    preparation_failures={**context.preparation_failures, "cloud": ["apply_infra"]},
                    preparation_errors={
                        "cloud": {
                            "phase": "infra",
                            "code": exc.code.value,
                            "detail": str(redact_obj(exc.message))[:1000],
                        }
                    },
                )
                subjects, infra_summary = {}, None
            guard()
            if digest_json(service.store.environments(original.project)) != baseline:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED,
                    "검토 중 배포 기준이 바뀌었습니다. 다시 준비하세요",
                )
            plan = _platform_bootstrap_plan(bundle.plan, context, infra_summary)
            return service.prepare(
                plan,
                context,
                source,
                patch=checked.patch,
                patch_meta=checked.meta,
                patch_review=checked.review,
                subjects=subjects,
                infra_summary=infra_summary,
                expected_settings_version=original.project_settings.get("version", 0),
                review_parent=(prepared.plan.run_id, draft["revision"]),
            )
        except (Exception, asyncio.CancelledError) as exc:
            failure = (
                DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "코드 수정 승인 자료 준비가 중단됐습니다"
                )
                if isinstance(exc, asyncio.CancelledError)
                else exc
            )
            service.record_preparation_failure(
                run_id, original.project, failure, context=context, phase="patch_review"
            )
            raise

    service.patch_reviews = PatchReviews(
        service,
        generate=generate,
        combine=combine_review,
        finalize=finalize,
        diff=proposal_diff,
    )


def _demo_reset_service(service, settings):
    from ddak.core.demo_backend import DemoBackend
    from ddak.core.demo_reset import DemoReset

    def urls(project, saved):
        result = {}
        path = saved.get("inventory_path") or os.environ.get("DDAK_ONPREM_INVENTORY")
        if path:
            with contextlib.suppress(DdakToolError, OSError, ValueError):
                result["local"] = load_inventory(Path(path)).get("public_url")
        if saved.get("cloud_domain"):
            result["cloud"] = "https://" + saved["cloud_domain"]
        return result

    return DemoReset(
        service,
        urls=urls,
        backend=DemoBackend(service.root, enabled=settings.adapter_mode is AdapterMode.REAL),
    )


def _code_question_service(service, settings):
    """대시보드 코드 질문. 소스 읽기는 코어, AI 답은 등록 툴 answer_code_question이 만든다."""
    from ddak.core.code_question import CodeQuestion
    from ddak.core.contracts.tools.answer_code_question import AnswerCodeQuestionOutput
    from ddak.core.registry import CODE_QUESTION
    from ddak.plan.analyze import CodeQuestionSession, code_question_session

    def answer(inp, ctx, effective):
        registered = service.registry.get(CODE_QUESTION)
        session = CodeQuestionSession(ctx.run_id, effective)
        with code_question_session(session), tool_context(CODE_QUESTION, ctx.run_id):
            output = registered.fn(inp, ctx)
        return AnswerCodeQuestionOutput.model_validate(output)

    return CodeQuestion(service, settings, answer=answer)


def create(
    *, cli_host: str | None = None, onprem_profile: bool = False, settings: Settings | None = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    # 직접 ASGI factory 기동은 실제 바인드를 알 수 없으므로 CLI에서 거부한다.
    require_local_cli(settings, host=cli_host)
    registry = load_tools()

    def deployment_factory():
        service = DeploymentService(
            registry,
            settings.run_dir.parent,
            refresh=_refresh_cloud_context,
            planning_flow=_manual_planning(settings, FetchPolicy.from_env()),
            repository_factory=_repository_factory(
                settings.run_dir.parent / "repositories",
                vault_root=settings.run_dir.parent / "private",
            ),
            build_preflight=_local_build_preflight,
        )
        service.onboarding = _setup_service(service, settings, cli_host)
        service.setup_actions = _setup_actions(service, settings)
        service.demo_reset = _demo_reset_service(service, settings)
        service.code_question = _code_question_service(service, settings)
        if onprem_profile:
            _configure_onprem(service)
        _configure_patch_review(service, settings)
        return service

    app = create_app(
        llm_status=llm_status,
        deployment_factory=deployment_factory,
        settings=settings,
    )
    _attach_watch(app, settings)
    from ddak.web.routes.ops import router as ops_router

    app.include_router(ops_router)
    from ddak.web.routes.setup import router as setup_router

    app.include_router(setup_router)
    from ddak.web.routes.setup_actions import router as setup_actions_router

    app.include_router(setup_actions_router)
    return app


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    # 외부에 열지 않는다(127.0.0.1). 온프렘 앱 기본 주소 8080과 겹치지 않게 8765.
    # 포트는 💭(설계 문서 00 N21, 하네스 I-26).
    uvicorn.run(create(cli_host=ADMIN_HOST), host=ADMIN_HOST, port=settings.admin_port)
