"""source=fixture: 감시 이관 시 취소된 실제 준비 경로의 계획 스레드를 끝까지 기다린다."""

import asyncio
import threading
from contextlib import suppress

import pytest
from fastapi import FastAPI

from ddak import app
from ddak.core.config import Settings
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from tests.unit.test_first_run import REPO
from tests.unit.test_first_run import first_run as first_run


@pytest.mark.anyio
@pytest.mark.parametrize("old_outcome", ["return", "raise"])
async def test_handoff_drains_planning_thread_despite_repeated_cancellation(
    first_run, monkeypatch, old_outcome
):
    service, _ = first_run
    service.onboarding = None  # AI/연결 검사는 이 스레드 수명 검사 대상이 아니다.
    source = {"repo_url": REPO, "watch_branch": "prod", "auto_detect": True}
    service.save_project_settings("old-app", source, updated_by="fixture", expected_version=0)
    loop = asyncio.get_running_loop()
    old_entered, new_entered, stopping, new_finished = (asyncio.Event() for _ in range(4))
    old_release, old_exited = threading.Event(), threading.Event()
    active_lock = threading.Lock()
    active = set()
    planning_threads_overlap = []
    watchers = {}

    def blocking_plan(request, **kwargs):
        project = request.project
        with active_lock:
            active.add(project)
            planning_threads_overlap.append(sorted(active))
        loop.call_soon_threadsafe((old_entered if project == "old-app" else new_entered).set)
        try:
            if project == "old-app":
                assert old_release.wait(5), "fixture 계획 스레드 해제 누락"
                if old_outcome == "return":
                    return object()  # 취소된 준비는 반환한 계획을 사용하지 않아야 한다.
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "fixture 계획 종료")
        finally:
            with active_lock:
                active.remove(project)
            if project == "old-app":
                old_exited.set()

    class StubWatcher:
        def __init__(self, targets, handler, **kwargs):
            self.target, self.handler = targets[0], handler
            self.stopped = asyncio.Event()
            self.callback = None
            watchers[self.target.project] = self

        async def deliver(self):
            try:
                await self.handler(self.target, "a" * 40)
            except DdakToolError:
                assert self.target.project == "new-app"
            finally:
                if self.target.project == "new-app":
                    new_finished.set()

        async def run(self):
            self.callback = asyncio.create_task(self.deliver())
            try:
                await self.stopped.wait()
            finally:
                self.callback.cancel()  # 실제 Watcher와 같이 handler 취소만 요청한다.

        async def stop(self):
            if self.target.project == "old-app":
                stopping.set()
            self.stopped.set()

    monkeypatch.setattr(app, "plan_deployment", blocking_plan)
    monkeypatch.setattr(app, "Watcher", StubWatcher)
    application = FastAPI()
    application.state.deployment = service
    app._attach_watch(application, Settings())
    async with application.router.lifespan_context(application):
        try:
            await asyncio.wait_for(old_entered.wait(), 3)
            service.save_project_settings(
                "new-app", source, updated_by="fixture", expected_version=0
            )
            await asyncio.wait_for(stopping.wait(), 3)
            old_callback = watchers["old-app"].callback
            for _ in range(3):
                old_callback.cancel()
                await asyncio.sleep(0)
            premature_finish = old_callback.done()
            # RED에서는 old 스레드가 살아 있는 채로 new 계획기가 진입한다.
            with suppress(TimeoutError):
                await asyncio.wait_for(new_entered.wait(), 0.2)
        finally:
            old_release.set()
        await asyncio.wait_for(new_finished.wait(), 3)

    assert old_exited.is_set()
    assert old_callback.cancelled()
    assert planning_threads_overlap == [["old-app"], ["new-app"]], planning_threads_overlap
    assert not premature_finish, "준비 coroutine이 계획 스레드보다 먼저 종료됐다"
    old_runs = [run for run in service.list_runs() if run["project"] == "old-app"]
    assert len(old_runs) == 1 and old_runs[0]["status"] == "FAILED_BEFORE_DEPLOY"
    assert "취소" in str(service.get_run(old_runs[0]["run_id"])["result"])
