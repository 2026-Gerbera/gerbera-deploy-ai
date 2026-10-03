"""source=fixture: 자동 감시가 실행 중 배포와 겹친 커밋을 버리지 않고 잠금 해제 뒤 준비한다.

데스크톱에서 PR merge가 진행 중 배포와 겹쳐 준비 run 3개가 LOCK_HELD로 실패하고 커밋이 사라졌다.
실제 Git/VM 접속 없음.
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from ddak import app
from ddak.core.config import Settings
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.web.app import create_app
from tests.unit import test_deployment_service as support

rig = support.rig
SHA = "a" * 40


def test_lock_held_marks_waiting_then_prepares_same_commit(rig, monkeypatch):
    service, source, _ = rig
    service.save_project_settings(
        "flaskr",
        {
            "repo_url": "https://github.com/fixture/app",
            "watch_branch": "prod",
            "auto_detect": True,
            "default_targets": "onprem",
        },
        updated_by="fixture",
        expected_version=0,
    )
    with service.store.connection() as db:  # 다른 실행이 잠금을 쥐고 있다
        now = time.time()
        db.execute(
            "INSERT INTO locks VALUES (?, ?, ?, ?, ?)",
            ("flaskr", "run-other", "lock-placeholder", now, now + 600),
        )
    prepared: list[str] = []
    handlers = []

    async def prepare(service, config, target, sha, **kwargs):
        prepared.append(sha)
        p = support.plan("run-after-lock").model_copy(update={"project": target.project})
        return service.prepare(p, app.RunContext(p.run_id, project=target.project), source)

    class FakeWatcher:
        def __init__(self, targets, handler, **kwargs):
            self.targets = targets
            self.stopped = asyncio.Event()
            handlers.append((targets[0], handler))

        async def run(self):
            await self.stopped.wait()

        async def stop(self):
            self.stopped.set()

    monkeypatch.setattr(app, "Watcher", FakeWatcher)
    monkeypatch.setattr(app, "_prepare_commit", prepare)
    application = create_app(deployment_factory=lambda: service)
    app._attach_watch(application, Settings())
    with TestClient(application, base_url="http://127.0.0.1:8765") as client:
        assert client.portal is not None

        async def started():
            for _ in range(200):
                if handlers:
                    return
                await asyncio.sleep(0.01)
            pytest.fail("fixture watch did not start")

        client.portal.call(started)
        target, handler = handlers[0]
        with pytest.raises(DdakToolError) as error:
            client.portal.call(handler, target, SHA)
        assert error.value.code is ErrorCode.LOCK_HELD
        assert prepared == [] and service.list_runs() == []  # 실패 준비 run을 만들지 않는다
        waiting = [r for r in service.list_preparations("flaskr") if r["status"] == "WAITING"]
        assert [r["detail"] for r in waiting] == ["다른 배포가 끝나면 이어서 준비"]
        page = client.get("/?project=flaskr").text
        assert "다른 배포가 끝나면 이어서 준비" in page and 'data-code="WAITING"' in page

        with service.store.connection() as db:  # 다른 실행이 끝났다
            db.execute("DELETE FROM locks WHERE project='flaskr'")
        client.portal.call(handler, target, SHA)
        assert prepared == [SHA]
        assert service.list_runs()[0]["status"] == "AWAITING_APPROVAL"
        assert not [r for r in service.list_preparations("flaskr") if r["status"] == "WAITING"]
        assert "다른 배포가 끝나면 이어서 준비" not in client.get("/?project=flaskr").text
