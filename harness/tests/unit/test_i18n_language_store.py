"""답변 언어 선호가 배포 설정·승인 스냅샷을 변경하지 않는 회귀."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ddak import app
from ddak.core.answer_language import request_language
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.store import Store
from ddak.executor.patch_review import PatchReviews
from ddak.plan.intake import FetchPolicy, WatchTarget
from tests.unit import test_deployment_service as support
from tests.unit.test_ui_integration_fix10 import PROJECT, client_for, post, prepare

rig = support.rig


def test_language_storage_does_not_create_settings_or_change_project_order(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    store.save_project_settings(
        "old", {"auto_detect": False}, updated_by="operator", expected_version=0
    )
    before = store.list_project_settings()
    store.remember_answer_language("new", "ja")
    assert store.project_settings("new") is None
    assert store.list_project_settings() == before
    assert Store(store.path).answer_language("new") == "ja"


@pytest.mark.parametrize("path", ["/ops/plan", "/settings/deploy"])
def test_language_save_failure_does_not_reject_post(rig, monkeypatch, path):
    service, _, _ = rig
    monkeypatch.setattr(
        service,
        "enqueue_deployment",
        lambda *a, **k: {"request_id": "new", "status": "PREPARING", "run_id": None},
    )
    save = Mock(side_effect=RuntimeError("private detail"))
    monkeypatch.setattr(service, "remember_answer_language", save)
    with client_for(service) as client:
        client.get(f"/?project={PROJECT}&lang=ja")
        response = post(client, path, project=PROJECT)
    assert response.status_code == 303
    assert "private detail" not in response.text
    save.assert_called_once_with(PROJECT, "ja")


def test_pending_patch_review_and_reused_request_preserve_language_and_settings(rig):
    service, source, _ = rig
    before = service.save_project_settings(
        PROJECT, {"auto_detect": False}, updated_by="operator", expected_version=0
    )
    rid = prepare(service, source)
    reviews = PatchReviews(service, generate=None, combine=None, finalize=None, diff=None)
    service.remember_answer_language(PROJECT, "ja")
    assert reviews._prepared(rid).context.project_settings["version"] == before["version"]
    with client_for(service) as client:
        client.get(f"/?project={PROJECT}&lang=ko")
        # 같은 준비 요청은 재사용한다. 새 언어를 기록하지 않는다.
        response = post(client, "/ops/plan", project=PROJECT)
    assert response.status_code == 303
    assert service.get_answer_language(PROJECT) == "ja"
    assert service.get_project_settings(PROJECT) == before
    assert reviews._prepared(rid).plan.run_id == rid


def test_rejected_request_never_records_language(rig, monkeypatch):
    service, _, _ = rig
    save = Mock()
    monkeypatch.setattr(service, "remember_answer_language", save)
    monkeypatch.setattr(
        service,
        "enqueue_deployment",
        Mock(side_effect=DdakToolError(ErrorCode.LOCK_HELD, "준비 중입니다")),
    )
    with client_for(service) as client:
        client.get(f"/?project={PROJECT}&lang=ja")
        response = post(client, "/ops/plan", project=PROJECT)
    assert response.status_code == 303
    save.assert_not_called()


def test_active_preparation_reuse_keeps_original_language(rig, monkeypatch):
    service, _, _ = rig
    started, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def request(project, **kwargs):
        seen.append(request_language.get())
        started.set()
        await release.wait()
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "fixture end")

    monkeypatch.setattr(service, "request_deployment", request)
    with client_for(service) as client:
        client.get(f"/?project={PROJECT}&lang=ja")
        assert post(client, "/ops/plan", project=PROJECT).status_code == 303
        client.portal.call(started.wait)
        client.get(f"/?project={PROJECT}&lang=ko")
        assert post(client, "/ops/plan", project=PROJECT).status_code == 303
        assert service.get_answer_language(PROJECT) == "ja"
        assert seen == ["ja"]
        client.portal.call(release.set)


@pytest.mark.anyio
@pytest.mark.parametrize("trigger, expected", [("auto", "ja"), ("manual", "ko")])
async def test_language_change_during_plan_preserves_version_and_run_snapshot(
    rig, monkeypatch, trigger, expected
):
    service, source, _ = rig
    before = service.save_project_settings(
        "demo",
        {"auto_detect": False, "ai_answer_language": "ja"},
        updated_by="operator",
        expected_version=0,
    )
    service.remember_answer_language("demo", "ja")
    seen = []

    def plan(request, **kwargs):
        seen.append(kwargs["source_context"].project_settings["ai_answer_language"])
        service.remember_answer_language("demo", "ko")
        p = support.plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
        return SimpleNamespace(
            plan=p, context=RunContext(p.run_id, project=p.project, mode=p.mode), source=source
        )

    monkeypatch.setattr(app, "plan_deployment", plan)
    token = request_language.set("ko")
    try:
        rid = await app._prepare_commit(
            service,
            Settings(),
            WatchTarget("demo", "https://github.com/org/app", "prod", "both"),
            "a" * 40,
            policy=FetchPolicy(root=source.parent),
            trigger=trigger,
        )
    finally:
        request_language.reset(token)
    assert service.get_run(rid)["status"] == "AWAITING_APPROVAL"
    assert seen == [expected]
    assert service.approval_view(rid)["project_settings"]["ai_answer_language"] == expected
    assert service.get_project_settings("demo") == before
    reviews = PatchReviews(service, generate=None, combine=None, finalize=None, diff=None)
    assert reviews._prepared(rid).context.project_settings["version"] == before["version"]
