"""미빌드 tier의 환경별 이월·승인 경계. Docker/AWS 없이 실제 서비스 장부를 쓴다."""

import pytest

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.release import CarriedImageSource, ImageObservation, ReleaseArtifacts
from ddak.core.registry import Registry, spec_for
from ddak.core.snapshots import digest_bytes, preview
from ddak.executor.engine import RunStatus
from ddak.executor.service import DeploymentService
from tests.unit import test_deployment_service as support
from tests.unit.test_deployment_service import Input, Output
from tests.unit.test_followup5_service import artifact

pytestmark = pytest.mark.anyio


@pytest.fixture
def image_rig(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("VERSION=1\n")
    registry = Registry(
        spec_for(n)
        for n in (
            "build_image",
            "deploy_tier",
            "smoke_test",
            "health_check",
            "rollback_tier",
            "compare_env_results",
            "prepare_db",
        )
    )
    calls = []

    @registry.tool("build_image")
    async def build(inp: Input, ctx: RunContext) -> Output:
        images = dict(ctx.release_artifacts.images) if ctx.release_artifacts else {}
        images[inp.tier] = artifact(ctx.run_id + inp.tier).model_copy(
            update={
                "platform_digests": {
                    p: digest_bytes((ctx.run_id + inp.tier + p).encode())
                    for p in ("linux/arm64", "linux/amd64")
                }
            }
        )
        return Output(release_artifacts=ReleaseArtifacts(snapshot=preview(source), images=images))

    @registry.tool("deploy_tier")
    @registry.tool("smoke_test")
    @registry.tool("health_check")
    async def deploy(inp: Input, ctx: RunContext) -> Output:
        assert inp.tier in ctx.images  # 실제 provider에 빈 이미지가 전달되면 안 된다.
        calls.append((inp.target.value, inp.tier, dict(ctx.images)))
        image = ctx.release_artifacts.images.get(inp.tier) if ctx.release_artifacts else None
        if image is None:
            # C2가 연결할 이월 관측 규약. 새 빌드 산출물에는 이 tier가 없어야 한다.
            previous = ctx.previous_release[inp.target.value]["image_sources"][inp.tier]
            image = CarriedImageSource.model_validate(previous).artifact
        assert image.ref == ctx.images[inp.tier]
        digest = image.platform_digests["linux/arm64"]
        return Output(observation=ImageObservation(platform="linux/arm64", platform_digest=digest))

    @registry.tool("rollback_tier")
    @registry.tool("compare_env_results")
    async def other(inp: Input, ctx: RunContext) -> Output:
        return Output()

    @registry.tool("prepare_db")
    async def migrate(inp: Input, ctx: RunContext) -> Output:
        calls.append((inp.target.value, "migrate", dict(ctx.images)))
        return Output()

    service = DeploymentService(registry, tmp_path / "state")
    yield service, source, calls
    service.close()


def both_plan(rid, *, full=False):
    p = support.plan(rid)
    if full:
        p.build.steps.insert(0, support.step("build.web", "build_image", tier="web"))
    for target in (Target.LOCAL, Target.CLOUD):
        getattr(p.deploy, target.value).steps.insert(
            0,
            support.step(
                f"deploy.web.{target.value}",
                "deploy_tier",
                target=target,
                tier="web",
                wait_for=["images_ready"],
            ),
        )
    return p


async def execute(service, source, p, *, targets=None):
    ctx = RunContext(p.run_id, project=p.project, targets=targets)
    service.prepare(p, ctx, source)
    service.approve(p.run_id, approver="operator")
    service.start(p.run_id)
    result = await service.wait(p.run_id)
    assert result.status is RunStatus.SUCCEEDED, result


async def test_partial_build_deploys_previous_web_per_target_and_records_observation(image_rig):
    service, source, calls = image_rig
    await execute(service, source, both_plan("v1-local", full=True), targets="onprem")
    await execute(service, source, both_plan("v1-cloud", full=True), targets="cloud")
    before = service.get_environments("demo")
    local = before["local"]["current"]["images"]["web"]
    cloud = before["cloud"]["current"]["images"]["web"]
    assert local != cloud
    calls.clear()
    (source / "app.py").write_text("VERSION=2\n")
    await execute(service, source, both_plan("v2"))
    for target, _tier, images in calls:
        assert images["web"] == {"local": local, "cloud": cloud}[target]
        assert images["was"] == artifact("v2was").ref
    after = service.get_environments("demo")
    for target in ("local", "cloud"):
        current = after[target]["current"]
        assert current["images"]["web"] == before[target]["current"]["images"]["web"]
        assert current["image_sources"]["web"]["carried_forward"]
        assert current["image_sources"]["web"]["release_id"] == f"v1-{target}"
        assert current["image_sources"]["web"]["observation"]["platform"] == "linux/arm64"
        assert set(current["artifacts"]["images"]) == {"was"}


async def test_missing_previous_image_fails_before_approval(image_rig):
    service, source, calls = image_rig
    p = both_plan("no-previous")
    with pytest.raises(DdakToolError, match="local/web"):
        service.prepare(p, RunContext(p.run_id, project=p.project), source)
    assert calls == [] and service.list_runs() == []


@pytest.mark.parametrize("legacy", ["missing", "null"])
async def test_carried_artifact_survives_prepared_restart_and_legacy_record(image_rig, legacy):
    import json

    service, source, _calls = image_rig
    await execute(service, source, both_plan("v1-cloud", full=True), targets="cloud")
    old = service.get_environments("demo")["cloud"]["current"]
    expected = old["artifacts"]["images"]["web"]
    old.pop("image_sources")  # 이전 버전 장부도 provider에는 새 고정 경로로 전달한다.
    if legacy == "null":
        old["image_sources"] = None
    with service.store.connection() as db:
        db.execute("UPDATE env_release SET current=? WHERE target='cloud'", (json.dumps(old),))
    p = both_plan("v2-cloud")
    service.prepare(p, RunContext(p.run_id, project=p.project, targets="cloud"), source)
    root, registry = service.root, service.registry
    service.close()
    restarted = DeploymentService(registry, root)
    try:
        restarted.approve(p.run_id, approver="operator")
        restarted.start(p.run_id)
        result = await restarted.wait(p.run_id)
        assert result.status is RunStatus.SUCCEEDED
        assert set(result.context.release_artifacts.images) == {"was"}
        origin = result.context.previous_release["cloud"]["image_sources"]["web"]
        assert (
            CarriedImageSource.model_validate(origin).artifact.model_dump(mode="json") == expected
        )
        current = restarted.get_environments("demo")["cloud"]["current"]
        assert (
            current["image_sources"]["web"]["observation"]["platform_digest"]
            == expected["platform_digests"]["linux/arm64"]
        )
        assert expected["index_digest"] != expected["platform_digests"]["linux/arm64"]
    finally:
        restarted.close()


@pytest.mark.parametrize("bad", ["missing", "ref", "platform"])
async def test_invalid_carried_artifact_stops_before_approval(image_rig, bad):
    import json

    service, source, _ = image_rig
    await execute(service, source, both_plan("v1", full=True))
    old = service.get_environments("demo")["cloud"]["current"]
    image = old["image_sources"]["web"]["artifact"]
    if bad == "missing":
        old["image_sources"]["web"].pop("artifact")
    elif bad == "ref":
        old["image_sources"]["web"]["artifact"] = artifact("other").model_dump(mode="json")
    else:
        image["platform_digests"].pop("linux/amd64")
    with service.store.connection() as db:
        db.execute("UPDATE env_release SET current=? WHERE target='cloud'", (json.dumps(old),))
    p = both_plan("invalid-carried")
    with pytest.raises(DdakToolError, match="cloud/web"):
        service.prepare(p, RunContext(p.run_id, project=p.project), source)
    assert p.run_id not in {r["run_id"] for r in service.list_runs()}


async def test_changed_previous_image_after_approval_cannot_start(image_rig):
    service, source, calls = image_rig
    await execute(service, source, both_plan("v1", full=True))
    p = both_plan("pending-v2")
    service.prepare(p, RunContext(p.run_id, project=p.project), source)
    service.approve(p.run_id, approver="operator")
    await execute(service, source, both_plan("another-v2", full=True))
    calls.clear()
    with pytest.raises(DdakToolError, match="승인 뒤 이월"):
        service.start(p.run_id)
    assert not calls


async def test_local_success_cannot_supply_missing_cloud_image(image_rig):
    service, source, _calls = image_rig
    await execute(service, source, both_plan("only-local", full=True), targets="onprem")
    p = both_plan("missing-cloud")
    with pytest.raises(DdakToolError, match="cloud/web"):
        service.prepare(p, RunContext(p.run_id, project=p.project), source)


@pytest.mark.parametrize("invalid", ["naked", "needs_human"])
async def test_carry_does_not_accept_unapproved_or_uncertain_state(image_rig, invalid):
    service, source, _calls = image_rig
    await execute(service, source, both_plan("baseline", full=True))
    if invalid == "needs_human":
        with service.store.connection() as db:
            db.execute("UPDATE env_release SET status='NEEDS_HUMAN' WHERE target='local'")
    p = both_plan("untrusted")
    ctx = RunContext(
        p.run_id,
        project=p.project,
        images={"web": artifact("unapproved").ref} if invalid == "naked" else {},
    )
    with pytest.raises(DdakToolError):
        service.prepare(p, ctx, source)


async def test_three_tier_plan_uses_locked_mysql_without_building_db(image_rig):
    import json

    from ddak.core.contracts.step_catalog import catalog_steps

    service, source, calls = image_rig
    mysql = artifact("official-mysql")
    mysql = mysql.model_copy(update={"ref": "docker.io/library/mysql@" + mysql.index_digest})
    (source / "images.lock.json").write_text(json.dumps({"mysql": mysql.model_dump(mode="json")}))
    ids = [s.id for s in catalog_steps(("web", "was", "db"), "local")]
    assert len(ids) == len(set(ids)) and "deploy.migrate.local" in ids and "deploy.db.local" in ids
    p = both_plan("three-tier", full=True)
    was, web = p.deploy.local.steps[1], p.deploy.local.steps[0]
    p.deploy.local.steps[:2] = [
        support.step(
            "deploy.db.local",
            "deploy_tier",
            tier="db",
            target=Target.LOCAL,
            wait_for=["images_ready"],
        ),
        support.step("deploy.migrate.local", "prepare_db", target=Target.LOCAL),
        was,
        web,
    ]
    await execute(service, source, p, targets="onprem")
    assert [tier for _, tier, _ in calls][:4] == ["db", "migrate", "was", "web"]
    assert calls[0][2]["db"] == mysql.ref
    current = service.get_environments("demo")["local"]["current"]
    assert current["images"]["db"] == mysql.ref
    assert current["artifacts"]["images"]["db"] == mysql.model_dump(mode="json")


async def test_missing_mysql_lock_stops_before_approval(image_rig):
    service, source, _calls = image_rig
    p = both_plan("missing-mysql", full=True)
    p.deploy.local.steps.insert(
        0, support.step("deploy.db.local", "deploy_tier", target=Target.LOCAL, tier="db")
    )
    with pytest.raises(DdakToolError, match=r"images\.lock\.json"):
        service.prepare(p, RunContext(p.run_id, project=p.project, targets="onprem"), source)


@pytest.mark.parametrize("invalid", ["build", "reference", "cloud"])
async def test_supplied_db_artifact_cannot_bypass_official_local_only_policy(image_rig, invalid):
    service, source, _calls = image_rig
    p = both_plan("db-policy", full=True)
    p.deploy.local.steps.insert(
        0, support.step("deploy.db.local", "deploy_tier", tier="db", target=Target.LOCAL)
    )
    mysql = artifact("mysql")
    if invalid != "reference":
        mysql = mysql.model_copy(update={"ref": "mysql@" + mysql.index_digest})
    if invalid == "build":
        p.build.steps.append(support.step("build.db", "build_image", tier="db"))
    if invalid == "cloud":
        p.deploy.cloud.steps.insert(
            0, support.step("deploy.db.cloud", "deploy_tier", tier="db", target=Target.CLOUD)
        )
    supplied = ReleaseArtifacts(snapshot=preview(source), images={"db": mysql})
    with pytest.raises(DdakToolError):
        service.prepare(
            p,
            RunContext(
                p.run_id,
                project=p.project,
                targets="both" if invalid == "cloud" else "onprem",
                release_artifacts=supplied,
            ),
            source,
        )


@pytest.mark.parametrize("invalid", ["tag", "mode"])
async def test_unpinned_or_wrong_mode_previous_image_is_not_approved(image_rig, invalid):
    import json

    service, source, _calls = image_rig
    await execute(service, source, both_plan("baseline", full=True))
    old = service.get_environments("demo")["cloud"]["current"]
    if invalid == "tag":
        old["images"]["web"] = "fixture.invalid/app:latest"
    else:
        old["source_mode"] = "real"
    with service.store.connection() as db:
        db.execute("UPDATE env_release SET current=? WHERE target='cloud'", (json.dumps(old),))
    p = both_plan("invalid-baseline")
    with pytest.raises(DdakToolError, match="cloud/web"):
        service.prepare(p, RunContext(p.run_id, project=p.project), source)


async def test_relative_service_and_store_keep_paths_after_cwd_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    service = DeploymentService(Registry([]), tmp_path.relative_to(tmp_path) / "state")
    try:
        monkeypatch.chdir(tmp_path.parent)
        assert service.root == tmp_path / "state"
        assert service.store.path == tmp_path / "state/ddak.sqlite"
        assert service.list_runs() == []
    finally:
        service.close()


@pytest.mark.parametrize("inventory", [None, {"tiers": {"web": {}}}])
async def test_real_local_inventory_is_checked_before_approval(image_rig, inventory):
    from ddak.core.config import AdapterMode

    service, source, calls = image_rig
    p = support.plan("inventory-precheck")
    ctx = RunContext(
        p.run_id,
        project=p.project,
        targets="onprem",
        adapter_mode=AdapterMode.REAL,
        platform={"onprem": inventory} if inventory else {},
    )
    with pytest.raises(DdakToolError, match="인벤토리"):
        service.prepare(p, ctx, source)
    assert service.list_runs() == [] and calls == []
