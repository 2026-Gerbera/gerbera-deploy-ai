"""수동 API: 요청 대상 기본값과 감시 브랜치/v* 태그 허용 목록."""

from types import SimpleNamespace

import pytest

from ddak import app
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError
from ddak.plan.intake import FetchPolicy
from tests.unit import test_deployment_service as support

rig = support.rig
pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "ref,resolved",
    [
        (None, "refs/heads/prod"),
        ("prod", "refs/heads/prod"),
        ("v1", "refs/tags/v1"),
        ("refs/tags/v2.0", "refs/tags/v2.0"),
    ],
)
async def test_manual_request_prepares_pinned_run_without_starting(rig, monkeypatch, ref, resolved):
    service, source, calls = rig
    service.save_project_settings(
        "demo",
        {"repo_url": "https://github.com/org/app", "default_targets": "onprem"},
        updated_by="operator",
        expected_version=0,
    )
    seen = []

    def resolve(url, ref, *, policy):
        seen.append(ref)
        return "a" * 40

    def plan(request, **kwargs):
        assert request.ref == "a" * 40
        p = support.plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
        return SimpleNamespace(
            plan=p,
            context=RunContext(p.run_id, project=p.project, mode=p.mode),
            source=source,
        )

    monkeypatch.setattr(app, "resolve_head", resolve)
    monkeypatch.setattr(app, "plan_deployment", plan)
    service.planning_flow = app._manual_planning(Settings(), FetchPolicy(root=source.parent))
    rid = await service.request_deployment("demo", ref=ref)
    assert service.get_run(rid)["status"] == "AWAITING_APPROVAL"
    view = service.approval_view(rid)
    assert view["trigger"] == "manual"
    assert view["targets"] == "onprem"
    assert view["plan"]["deploy"]["cloud"]["steps"] == []
    assert seen == [resolved]
    assert not calls.contexts


@pytest.mark.parametrize(
    "ref", ["main", "ai-prod", "deadbeef", "refs/heads/v1", "v../x", "v/x.lock", "v//x"]
)
async def test_other_refs_are_rejected_before_planning(rig, ref):
    service, _, _ = rig
    service.save_project_settings(
        "demo",
        {"repo_url": "https://github.com/org/app"},
        updated_by="operator",
        expected_version=0,
    )

    async def forbidden(*args):
        pytest.fail("invalid ref reached planning")

    service.planning_flow = forbidden
    with pytest.raises(DdakToolError, match="v\\*"):
        await service.request_deployment("demo", ref=ref)
    assert not service.list_runs()


async def test_explicit_targets_override_request_defaults(rig):
    service, _, _ = rig
    service.save_project_settings(
        "demo",
        {
            "repo_url": "https://github.com/org/app",
            "default_targets": "onprem",
            "watch_branch": "release/prod",
        },
        updated_by="operator",
        expected_version=0,
    )
    seen = []

    async def prepare(service, request):
        seen.append(request)
        return "requested-run"

    service.planning_flow = prepare
    assert await service.request_deployment("demo", targets="cloud") == "requested-run"
    assert seen[0].target == "cloud" and seen[0].ref == "release/prod"


@pytest.mark.parametrize("length,allowed", [(190, True), (191, False)])
async def test_tag_length_includes_fully_qualified_prefix(rig, length, allowed):
    service, _, _ = rig
    service.save_project_settings(
        "demo",
        {"repo_url": "https://github.com/org/app"},
        updated_by="operator",
        expected_version=0,
    )
    seen = []

    async def plan(service, request):
        seen.append(request.ref)
        return "run-tag"

    service.planning_flow = plan
    tag = "v" + "a" * (length - 1)
    if allowed:
        assert await service.request_deployment("demo", ref=tag) == "run-tag"
        assert len(seen[0]) == 200
    else:
        with pytest.raises(DdakToolError):
            await service.request_deployment("demo", ref=tag)
        assert not seen
