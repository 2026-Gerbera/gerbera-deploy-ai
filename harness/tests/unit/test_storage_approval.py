"""source=fixture: v3에서 v1으로 복구할 때 제거 의도가 승인까지 유지된다."""

from dataclasses import replace

import pytest
from markupsafe import escape

from ddak import app
from ddak.cloud.infra import unbind_infra
from ddak.cloud.infra.storage_allocation import prepare_storage_context
from ddak.core.config import AdapterMode, Settings
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.tools.plan_infra import PlanInfraInput
from ddak.core.registry import Registry, spec_for
from ddak.executor.infra import refresh_infra_context
from ddak.plan.intake import FetchPolicy, WatchTarget
from ddak.web.dependencies import templates
from ddak.web.story import approval_story
from tests.unit.cloud.infra.test_storage_runtime import context
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_infra_preparation import infra_plan

pytestmark = pytest.mark.anyio


def infra_registry(service):
    loaded = app.load_tools()
    names = ("validate_infra", "plan_infra", "apply_infra")
    registry = Registry([*service.registry.specs, *(spec_for(n) for n in names)])
    for name in service.registry.registered():
        registry.tool(name)(service.registry.get(name).fn)
    for name in names:
        registry.tool(name)(loaded.get(name).fn)
    service.registry = registry
    service.refresh = refresh_infra_context


def storage_block(view):
    """승인 화면의 S3 판단 흐름 블록. 실제 storage 요약이 그대로 화면까지 오는지 본다."""
    html = templates.get_template("approval.html").render(
        approval=view,
        story=approval_story(view),
        project=view.get("project"),
        csrf_token="fixture",
        request={"url": {"path": "/approval"}},
    )
    block = html.split('id="resource-decision-slot"', 1)[1].split("</section>", 1)[0]
    storage = view["infra_summary"]["storage"]
    assert len(storage["rationale"]) == 5
    assert all(str(escape(text)) in block for text in storage["rationale"])
    assert "판단 근거 기록 없음" not in block
    return block


async def test_v3_to_v1_remove_is_shown_before_approval(rig):
    service, source, _ = rig
    infra_registry(service)
    plan = infra_plan("storage-reset")
    ctx = replace(
        context("remove"),
        run_id=plan.run_id,
        project=plan.project,
        project_settings={**context("remove").project_settings, "cloud_platform": "flaskr"},
    )
    try:
        subjects, metadata = await app._infra_approval(service, plan, ctx)
        run_id = service.prepare(plan, ctx, source, subjects=subjects, infra_summary=metadata)
        view = service.approval_view(run_id)
        assert service.get_run(run_id)["status"] == "AWAITING_APPROVAL"
        assert view["infra_summary"]["counts"]["delete"] == 4
        assert view["infra_summary"]["storage"]["intent"] == "remove"
        block = storage_block(view)
        bucket = view["infra_summary"]["storage"]["bucket"]
        assert f"S3 이미지 저장소 삭제 · {bucket} (업로드 이미지 포함)" in block
        assert "생성 0 · 삭제 4" in block
        assert not service.get_approvals(run_id)
        assert "123456789012" not in str(view["infra_summary"])
    finally:
        unbind_infra(plan.run_id)


async def test_reserved_name_is_displayed_and_bound_before_approval(rig):
    from ddak.cloud.infra import run_plan

    service, source, _ = rig
    infra_registry(service)
    plan = infra_plan("storage-create")
    ctx = replace(
        context(),
        run_id=plan.run_id,
        project=plan.project,
        project_settings={
            "cloud_platform": "flaskr",
            "_infra_storage": {
                "intent": "create",
                "evidence": [{"file": "flaskr/uploads.py", "line": 3, "kind": "hardcoded_dir"}],
            },
        },
    )
    ctx = prepare_storage_context(ctx, service.root)
    try:
        subjects, metadata = await app._infra_approval(service, plan, ctx)
        run_id = service.prepare(plan, ctx, source, subjects=subjects, infra_summary=metadata)
        view = service.approval_view(run_id)
        assert service.get_run(run_id)["status"] == "AWAITING_APPROVAL"
        assert view["infra_summary"]["storage"]["bucket"] == "gerbera-flaskr-images-1"
        block = storage_block(view)
        assert "S3 이미지 저장소 신규 생성 · gerbera-flaskr-images-1" in block
        assert "생성 4 · 삭제 0" in block and "s3://gerbera-flaskr-images-1/img" in block
        assert view["project_settings"]["_infra_storage"]["bucket"] == "gerbera-flaskr-images-1"
        changed = replace(
            ctx,
            project_settings={
                **ctx.project_settings,
                "_infra_storage": {
                    **ctx.project_settings["_infra_storage"],
                    "bucket": "gerbera-flaskr-images-2",
                },
            },
        )
        with pytest.raises(DdakToolError, match="인프라 세션"):
            run_plan(PlanInfraInput(run_id=run_id), changed)
    finally:
        unbind_infra(plan.run_id)


async def test_auto_prepare_preserves_storage_intent_from_plan(rig, monkeypatch):
    from types import SimpleNamespace

    service, source, _ = rig
    infra_registry(service)
    seen = []
    service.store.save_project_settings(
        "demo", {"cloud_platform": "flaskr"}, updated_by="operator", expected_version=0
    )

    def planned(request, **kwargs):
        plan = infra_plan(kwargs["run_id"]).model_copy(update={"mode": RunMode.UPDATE})
        ctx = replace(
            context("remove"),
            run_id=plan.run_id,
            project=plan.project,
            project_settings={
                **kwargs["source_context"].project_settings,
                "_infra_storage": context("remove").project_settings["_infra_storage"],
            },
        )
        return SimpleNamespace(plan=plan, context=ctx, source=source)

    async def capture(service, plan, ctx):
        seen.append(ctx.project_settings["_infra_storage"])
        return {}, None

    monkeypatch.setattr(
        service,
        "get_platform_outputs",
        lambda *_: context("remove").platform["cloud"],
    )
    monkeypatch.setattr(app, "plan_deployment", planned)
    monkeypatch.setattr(app, "_infra_approval", capture)
    await app._prepare_commit(
        service,
        Settings(adapter_mode=AdapterMode.FAKE),
        WatchTarget("demo", "https://github.com/org/app", "prod", "cloud"),
        "a" * 40,
        policy=FetchPolicy(root=source.parent),
    )
    assert seen and seen[0]["intent"] == "remove"
