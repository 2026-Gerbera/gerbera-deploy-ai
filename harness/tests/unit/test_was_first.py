"""배포 순서 WAS 먼저(10/3): 카탈로그 → 계획 검증 → 실행기 호출 순서·롤백 역순.

데모 앱 deploy.yaml의 tiers는 web, was 순서다. 계획은 deploy.yaml 순서와 무관하게
업스트림(was)을 먼저 배포하고, 롤백은 실제 호출의 역순(web → was)이다.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Plan
from ddak.core.contracts.plan_facts import EnvKey, Facts
from ddak.core.contracts.release import CarriedImageSource, ImageObservation, ReleaseArtifacts
from ddak.core.contracts.step_catalog import catalog_steps
from ddak.core.contracts.tools.validate_plan import ValidatePlanInput
from ddak.core.registry import Registry, spec_for
from ddak.core.snapshots import digest_bytes, preview
from ddak.executor.engine import RunStatus, check_signals
from ddak.executor.service import DeploymentService
from ddak.plan.validate import validate_plan
from tests.unit.test_deployment_service import Input, Output
from tests.unit.test_followup5_service import artifact

H = "sha256:" + "b" * 64
DEMO_TIERS = ("web", "was")  # 데모 앱 deploy.yaml 순서 그대로
RUNTIME_TOOLS = {"deploy_tier", "prepare_db", "health_check", "smoke_test"}


class DbInput(Input):
    migrations: tuple[str, ...] = ()


def _deploy_tiers(steps: Any) -> list[str]:
    return [s.tier for s in steps if s.tool == "deploy_tier"]


@pytest.mark.parametrize("tiers", [("web", "was"), ("was", "web")])
@pytest.mark.parametrize("env", ["local", "cloud"])
def test_catalog_deploys_was_before_web_regardless_of_deploy_yaml_order(tiers, env):
    ids = [s.id for s in catalog_steps(tiers, "both")]
    assert ids.index(f"deploy.migrate.{env}") < ids.index(f"deploy.was.{env}")
    assert ids.index(f"deploy.was.{env}") < ids.index(f"deploy.web.{env}")
    assert ids.index(f"deploy.web.{env}") < ids.index(f"verify.health.{env}")
    # 빌드는 순서 의존이 없다(images_ready 하나로 모인다). deploy.yaml 순서를 유지한다.
    assert [i for i in ids if i.startswith("build.")] == [f"build.{t}" for t in tiers]


def test_unknown_tiers_follow_known_tiers_in_deploy_yaml_order():
    steps = catalog_steps(("worker", "web", "api", "was", "db"), "local")
    assert _deploy_tiers(steps) == ["db", "was", "web", "worker", "api"]
    assert _deploy_tiers(catalog_steps(("web",), "local")) == ["web"]


def _facts(**kw: Any) -> Facts:
    base: dict[str, Any] = dict(
        project="demo", mode=RunMode.UPDATE, target="local", tiers=DEMO_TIERS,
        changed={"local": {"web": True, "was": True}}, new_migrations=("0001",),
        env_keys=(EnvKey(name="SECRET_KEY", kind="secret", tier=None),),
        db_initialized={"local": False}, infra_inputs_changed=False,
        source_snapshot_hash=H, facts_hash=H,
    )  # fmt: skip
    return Facts(**{**base, **kw})


V1 = _facts()  # 첫 배포: 두 tier 모두 빌드·배포, 0001 마이그레이션
V2 = _facts(  # PR merge: WAS만 변경, web은 이월
    changed={"local": {"web": False, "was": True}}, new_migrations=(), env_keys=(),
    db_initialized={"local": True},
)  # fmt: skip


def _rule_plan(facts: Facts, run_id: str) -> Plan:
    ctx = RunContext(run_id, toggles={"code_patch": False})
    return validate_plan(ValidatePlanInput(run_id=run_id, facts=facts), ctx)


def test_demo_v1_rule_plan_deploys_was_then_web():
    p = _rule_plan(V1, "run-v1")
    check_signals(p)
    local = [s.id for s in p.deploy.local.steps]
    assert _deploy_tiers(p.deploy.local.steps) == ["was", "web"]
    assert local.index("deploy.migrate.local") < local.index("deploy.was.local")
    assert [s.tier for s in p.build.steps] == ["web", "was"]


def test_demo_v2_was_only_build_keeps_web_deploy_skipped():
    p = _rule_plan(V2, "run-v2")
    assert [s.tier for s in p.build.steps] == ["was"]
    assert _deploy_tiers(p.deploy.local.steps) == ["was"]
    assert {s.id: s.skip_rule for s in p.deploy.local.skipped}["deploy.web.local"] == (
        "digest_deployed"
    )


def test_demo_v2_new_shared_key_redeploys_carried_web_after_was():
    keys = (EnvKey(name="IMAGE_BOX_LIMIT", kind="plain", tier=None),)
    p = _rule_plan(V2.model_copy(update={"env_keys": keys}), "run-v2-key")
    assert [s.tier for s in p.build.steps] == ["was"]
    assert _deploy_tiers(p.deploy.local.steps) == ["was", "web"]


def _runtime_plan(facts: Facts, run_id: str) -> Plan:
    """규칙 계획의 순서를 그대로 두고 이 리그에 등록한 툴만 남긴다."""
    rule = _rule_plan(facts, run_id)
    steps = [s for s in rule.deploy.local.steps if s.tool in RUNTIME_TOOLS]
    return Plan.model_validate(
        {
            "run_id": run_id,
            "project": "demo",
            "toggles": {"code_patch": False},
            "build": {"steps": rule.build.steps, "signal": "images_ready"},
            "deploy": {"local": {"steps": steps}},
        }
    )


class Rig:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.deployed: list[str | None] = []
        self.rolled_back: list[str | None] = []
        self.fail_deploy: str | None = None
        self.fail_smoke = False


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[tuple[DeploymentService, Rig]]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("VERSION=1\n")
    state = Rig(source)
    registry = Registry(
        spec_for(n)
        for n in (
            "build_image",
            "deploy_tier",
            "prepare_db",
            "health_check",
            "smoke_test",
            "rollback_tier",
        )
    )

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
    async def deploy(inp: Input, ctx: RunContext) -> Output:
        state.deployed.append(inp.tier)
        if inp.tier == state.fail_deploy:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "injected deploy failure")
        image = ctx.release_artifacts.images.get(inp.tier) if ctx.release_artifacts else None
        if image is None:
            previous = ctx.previous_release[inp.target.value]["image_sources"][inp.tier]
            image = CarriedImageSource.model_validate(previous).artifact
        digest = image.platform_digests["linux/arm64"]
        return Output(observation=ImageObservation(platform="linux/arm64", platform_digest=digest))

    @registry.tool("prepare_db")
    async def prepare_db(inp: DbInput, ctx: RunContext) -> Output:
        return Output()

    @registry.tool("health_check")
    async def health(inp: Input, ctx: RunContext) -> Output:
        return Output()

    @registry.tool("smoke_test")
    async def smoke(inp: Input, ctx: RunContext) -> Output:
        return Output(passed=not state.fail_smoke)

    @registry.tool("rollback_tier")
    async def rollback(inp: Input, ctx: RunContext) -> Output:
        state.rolled_back.append(inp.tier)
        return Output()

    service = DeploymentService(registry, tmp_path / "state")
    try:
        yield service, state
    finally:
        service.close()


async def _run(service: DeploymentService, rig: Rig, p: Plan) -> RunStatus:
    service.prepare(p, RunContext(p.run_id, project=p.project, targets="onprem"), rig.source)
    service.approve(p.run_id, approver="operator")
    service.start(p.run_id)
    return (await service.wait(p.run_id)).status


@pytest.mark.anyio
async def test_executor_deploys_was_first_and_rolls_back_in_reverse(rig):
    service, state = rig
    assert await _run(service, state, _runtime_plan(V1, "run-v1")) is RunStatus.SUCCEEDED
    assert state.deployed == ["was", "web"]

    state.deployed.clear()
    state.fail_smoke = True
    (state.source / "app.py").write_text("VERSION=2\n")
    assert await _run(service, state, _runtime_plan(V1, "run-v1b")) is not RunStatus.SUCCEEDED
    assert state.deployed == ["was", "web"]
    assert state.rolled_back == ["web", "was"]  # 실제 호출의 역순


@pytest.mark.anyio
async def test_was_failure_leaves_web_untouched(rig):
    service, state = rig
    assert await _run(service, state, _runtime_plan(V1, "run-v1")) is RunStatus.SUCCEEDED
    before = service.get_environments("demo")["local"]["current"]["images"]["web"]

    state.deployed.clear()
    state.fail_deploy = "was"
    (state.source / "app.py").write_text("VERSION=2\n")
    assert await _run(service, state, _runtime_plan(V1, "run-v1b")) is not RunStatus.SUCCEEDED
    assert state.deployed == ["was"]  # web은 호출되지 않는다
    assert state.rolled_back == ["was"]  # 롤백 범위가 WAS로 한정된다
    assert service.get_environments("demo")["local"]["current"]["images"]["web"] == before


@pytest.mark.anyio
async def test_v2_was_only_build_with_carried_web_keeps_order_and_reverse_rollback(rig):
    service, state = rig
    assert await _run(service, state, _runtime_plan(V1, "run-v1")) is RunStatus.SUCCEEDED
    web_v1 = service.get_environments("demo")["local"]["current"]["images"]["web"]

    state.deployed.clear()
    (state.source / "app.py").write_text("VERSION=2\n")
    keys = (EnvKey(name="IMAGE_BOX_LIMIT", kind="plain", tier=None),)
    v2 = _runtime_plan(V2.model_copy(update={"env_keys": keys}), "run-v2")
    assert [s.tier for s in v2.build.steps] == ["was"]
    assert await _run(service, state, v2) is RunStatus.SUCCEEDED
    assert state.deployed == ["was", "web"]
    current = service.get_environments("demo")["local"]["current"]["images"]
    assert current["web"] == web_v1  # 이월
    assert current["was"] != web_v1

    state.deployed.clear()
    state.fail_smoke = True
    (state.source / "app.py").write_text("VERSION=3\n")
    v3 = _runtime_plan(V2.model_copy(update={"env_keys": keys}), "run-v3")
    assert await _run(service, state, v3) is not RunStatus.SUCCEEDED
    assert state.deployed == ["was", "web"]
    assert state.rolled_back == ["web", "was"]
