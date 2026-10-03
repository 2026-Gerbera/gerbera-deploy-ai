"""부분 빌드 장부 및 승인 snapshot 전달 회귀."""

from dataclasses import replace

import pytest

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.release import ImageArtifact, ReleaseArtifacts
from ddak.core.snapshots import digest_bytes, preview
from ddak.executor.engine import RunStatus
from tests.unit import test_deployment_service as support

rig = support.rig
pytestmark = pytest.mark.anyio


def artifact(label):
    sha = digest_bytes(label.encode())
    return ImageArtifact(
        ref="fixture.invalid/app@" + sha,
        index_digest=sha,
        platform_digests={"linux/arm64": sha, "linux/amd64": sha},
    )


async def deploy(service, source, calls, rid, tiers, fail=False):
    calls.artifacts = ReleaseArtifacts(
        snapshot=preview(source), images={t: artifact(rid + t) for t in tiers}
    )
    p = support.plan(rid)
    if "web" in tiers:
        p.build.steps.insert(0, support.step("build.web", "build_image", tier="web"))
        for section in (p.deploy.local, p.deploy.cloud):
            section.steps.insert(
                0,
                section.steps[0].model_copy(
                    update={"id": section.steps[0].id.replace("was", "web"), "tier": "web"}
                ),
            )
    service.prepare(p, RunContext(rid, project="demo"), source)
    service.approve(rid, approver="operator")
    calls.fail_cloud = fail
    service.start(rid)
    return await service.wait(rid)


async def test_partial_build_preserves_tier_origin_for_rollback_and_next_deploy(rig):
    service, source, calls = rig
    assert (
        await deploy(service, source, calls, "full-v1", ["web", "was"])
    ).status is RunStatus.SUCCEEDED
    initial = service.get_environments("demo")["cloud"]["current"]
    (source / "app.py").write_text("VERSION=2\n")
    assert (
        await deploy(service, source, calls, "partial-v2", ["was"])
    ).status is RunStatus.SUCCEEDED
    partial = service.get_environments("demo")["cloud"]["current"]
    assert partial["images"]["web"] == initial["images"]["web"]
    assert partial["image_sources"]["web"]["release_id"] == "full-v1"
    assert partial["image_sources"]["web"]["carried_forward"] is True
    assert partial["image_sources"]["web"]["artifact"] == initial["artifacts"]["images"]["web"]
    # 新 snapshot는 새 build만 뜻한다. inherited artifact는 origin에 별도 기록한다.
    assert set(partial["artifacts"]["images"]) == {"was"}
    result = await deploy(service, source, calls, "failed-v3", ["web", "was"], fail=True)
    assert result.status is RunStatus.FAILED_CLOUD
    assert service.get_environments("demo")["cloud"]["current"] == partial
    rollback = next(ctx for name, ctx in reversed(calls.contexts) if name == "rollback.cloud")
    assert rollback.previous_release["cloud"]["images"]["web"] == initial["images"]["web"]
    assert (
        await deploy(service, source, calls, "full-v4", ["web", "was"])
    ).status is RunStatus.SUCCEEDED
    current = service.get_environments("demo")["cloud"]["current"]
    assert current["image_sources"]["web"]["release_id"] == "full-v4"
    assert not current["image_sources"]["web"]["carried_forward"]


async def test_approved_binding_reaches_build_and_caller_cannot_preset(rig):
    from ddak.core.contracts.errors import DdakToolError

    service, source, calls = rig
    binding = preview(source)
    p = support.plan("binding-run")
    with pytest.raises(DdakToolError, match="source_binding"):
        service.prepare(p, RunContext(p.run_id, project="demo", source_binding=binding), source)
    service.prepare(p, RunContext(p.run_id, project="demo"), source)
    service.approve(p.run_id, approver="operator")
    service.start(p.run_id)
    await service.wait(p.run_id)
    built = next(ctx for name, ctx in calls.contexts if name == "build")
    assert built.source_binding == binding
    assert built.to_json_dict()["source_binding"] == binding.model_dump(mode="json")


@pytest.mark.parametrize("change", ["binding", "artifact"])
async def test_refresh_cannot_change_approved_binding_or_artifact(rig, change):
    service, source, calls = rig
    other = preview(source).model_copy(
        update={
            "source_snapshot_hash": digest_bytes(b"other"),
            "build_snapshot_hash": digest_bytes(b"other"),
        }
    )
    service.refresh = lambda step, out, ctx: replace(
        ctx,
        **(
            {"source_binding": other}
            if change == "binding"
            else {"release_artifacts": ReleaseArtifacts(snapshot=other, images={})}
        ),
    )
    rid = support.prepare(service, source)
    service.approve(rid, approver="operator")
    service.start(rid)
    result = await service.wait(rid)
    assert result.status is RunStatus.FAILED_BEFORE_DEPLOY
    assert not any(n.startswith("deploy.") for n, _ in calls.contexts)
    errors = " ".join(r.error or "" for r in result.records)
    assert ("실행 식별자" if change == "binding" else "source_binding") in errors


async def test_fake_repo_factory_prepares_and_executes_without_git_writes(rig, monkeypatch):
    from ddak import app
    from ddak.core.app_repository import AppRepository

    service, source, _calls = rig
    service.repository_factory = app._repository_factory(service.root / "fake-repos")
    monkeypatch.setattr(
        AppRepository, "git_bytes", lambda *a, **kw: pytest.fail("no Git in fake candidate/publish")
    )
    p = support.plan("fake-repo-run")
    ctx = RunContext(
        p.run_id, project="demo", repo_url="https://github.com/fixture/app", source_sha="a" * 40
    )
    service.connect_repository(ctx)
    service.prepare(p, ctx, source)
    service.approve(p.run_id, approver="operator")
    service.start(p.run_id)
    result = await service.wait(p.run_id)
    assert result.status is RunStatus.SUCCEEDED
    assert result.context.candidate_sha
    assert service.get_release(p.run_id)["git"]["status"] == "SIMULATED"
    assert service.get_release(p.run_id)["git"]["source"] == "fake"


async def test_platform_outputs_reach_next_request_and_fake_never_seeds_real(rig, monkeypatch):
    from types import SimpleNamespace

    from ddak import app
    from ddak.core.config import AdapterMode, Settings
    from ddak.plan.intake import FetchPolicy, WatchTarget

    service, source, _calls = rig
    outputs = {
        "codebuild_project_name": "ddak-project",
        "image_repository": "fixture/app",
        "cluster_arn": "arn:aws:ecs:ap-northeast-2:123456789012:cluster/demo",
        "rds_master_secret_arn": (
            "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:rds!demo"
        ),
    }
    p = support.plan("platform-seed")
    service.prepare(p, RunContext(p.run_id, project="demo", platform={"cloud": outputs}), source)
    service.approve(p.run_id, approver="operator")
    service.start(p.run_id)
    await service.wait(p.run_id)
    assert service.get_platform_outputs("demo", AdapterMode.FAKE) == outputs
    assert service.get_platform_outputs("demo", AdapterMode.REAL) == {}
    seen = []

    def plan(request, **kwargs):
        seen.append(kwargs["platform"])
        p = support.plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
        return SimpleNamespace(
            plan=p, context=RunContext(p.run_id, project="demo", mode=p.mode), source=source
        )

    monkeypatch.setattr(app, "plan_deployment", plan)
    rid = await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", "https://github.com/fixture/app", "prod", "both"),
        "a" * 40,
        policy=FetchPolicy(root=source.parent),
    )
    expected = {**outputs, "region": "ap-northeast-2"}
    assert seen == [{"cloud": expected}]
    assert service.approval_view(rid)["run_id"] == rid
    assert service._load_prepared(rid).context.platform["cloud"] == expected


@pytest.mark.parametrize("missing", [False, True])
async def test_repository_failure_is_recorded_before_execution(rig, missing):
    from ddak.core.contracts.errors import DdakToolError, ErrorCode

    service, source, calls = rig

    def factory(ctx):
        if missing:
            return None
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "origin differs")

    service.repository_factory = factory
    from ddak.core.config import AdapterMode
    from ddak.core.contracts.enums import RunMode

    p = support.plan("repository-error").model_copy(update={"mode": RunMode.BOOTSTRAP})
    p.deploy.cloud.steps.clear()
    p.verify.steps.clear()
    p.deploy.local.steps.insert(
        1, support.step("health.local", "health_check", target="local", tier="was")
    )
    rid = service.prepare(
        p,
        RunContext(
            p.run_id,
            project=p.project,
            adapter_mode=AdapterMode.REAL,
            platform={"onprem": {"tiers": {"was": {}}}},
            mode=RunMode.BOOTSTRAP,
            source_sha="a" * 40,
        ),
        source,
    )
    service.approve(rid, approver="operator")
    service.start(rid)
    assert (await service.wait(rid)).status is RunStatus.FAILED_BEFORE_DEPLOY
    recorded = service.get_release(rid)["git"]
    assert recorded["status"] == "FAILED" and recorded["phase"] == "repository"
    assert recorded["code"] == ("CONFIG_INVALID" if missing else "PRECONDITION_FAILED")
    assert not calls.contexts


async def test_tag_host_policy_is_checked_before_repository_connection(rig, monkeypatch):
    from ddak import app
    from ddak.core.config import Settings
    from ddak.plan.intake import FetchPolicy, WatchTarget

    service, source, _ = rig
    monkeypatch.setattr(service, "connect_repository", lambda *a: pytest.fail("must not clone"))
    rid = await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", "https://unapproved.invalid/org/app", "refs/tags/v1"),
        None,
        trigger="manual",
        policy=FetchPolicy(root=source.parent),
    )
    result = service.get_run(rid)["result"]
    assert result["phase"] == "resolve" and result["code"] == "CONFIG_INVALID"
