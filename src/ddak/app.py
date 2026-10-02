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
import importlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from ddak.cd import configure_cloud_tls
from ddak.cloud.tls import ensure_tls
from ddak.core.ai.status import llm_status
from ddak.core.config import Settings
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.plan_facts import FileMeta
from ddak.core.logging import get_logger
from ddak.core.registry import REGISTRY, Registry, import_tools
from ddak.executor.infra import refresh_infra_context
from ddak.executor.service import DeploymentService
from ddak.plan import new_run_id, plan_deployment
from ddak.plan.intake import FetchPolicy, Watcher, WatchTarget, load_watch_targets
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


def _attach_watch(app: FastAPI, settings: Settings) -> None:
    """DDAK_WATCH_REPO_URL이 있을 때만 앱 lifespan에 저장소 감시를 붙인다(web/app.py는 그대로)."""
    if not os.environ.get("DDAK_WATCH_REPO_URL"):
        _log.info("감시 비활성: DDAK_WATCH_REPO_URL 없음")
        return
    policy = FetchPolicy.from_env()
    inner = app.router.lifespan_context

    async def on_new_commit(t: WatchTarget, sha: str) -> None:
        # 계획 생성·검증까지만 자동. 배포는 사람 승인 뒤 실행기 몫이다(승인 UI 연결은 C3).
        store = app.state.deployment.store
        prev = _previous_manifests(store, t.project)
        mode = RunMode.UPDATE if any(v is not None for v in prev.values()) else RunMode.BOOTSTRAP
        request = DeployRequest(
            project=t.project, repo_url=t.repo_url, ref=t.ref, target=t.target, mode=mode
        )
        run_id = new_run_id()
        bundle = await asyncio.to_thread(
            plan_deployment,
            request,
            run_id=run_id,
            settings=settings,
            previous_manifests=lambda project: _previous_manifests(store, project),
            fetch_policy=policy,
        )
        _log.info(
            "새 커밋 계획 생성, 승인 대기",
            run_id=run_id,
            project=t.project,
            commit=sha,
            plan_fallback=bool(bundle.plan.planner and bundle.plan.planner.fallback),
        )

    @asynccontextmanager
    async def lifespan(a: FastAPI) -> AsyncIterator[Any]:
        async with inner(a) as state:
            watcher = Watcher(load_watch_targets(), on_new_commit, policy=policy)
            task = asyncio.create_task(watcher.run(), name="repo-watch")
            try:
                yield state
            finally:
                await watcher.stop()
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)

    app.router.lifespan_context = lifespan


def create() -> FastAPI:
    registry = load_tools()
    settings = Settings.from_env()
    app = create_app(
        llm_status=llm_status,
        deployment_factory=lambda: DeploymentService(
            registry, settings.run_dir.parent, refresh=refresh_infra_context
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
