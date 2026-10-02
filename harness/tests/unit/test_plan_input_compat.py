"""O2의 결정 결과와 마이그레이션 경고를 재해석 없이 실행·승인에 보존한다."""

import pytest
from pydantic import Field

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.plan import Invalidated, PlanWarning, SkippedStep
from ddak.core.registry import Registry, spec_for
from ddak.executor.engine import RunStatus
from ddak.executor.service import DeploymentService
from tests.unit import test_deployment_service as support

rig = support.rig
pytestmark = pytest.mark.anyio


class MigrationInput(support.Input):
    migrations: list[str] = Field(default_factory=list)


@pytest.mark.parametrize("forced_skip", [False, True])
async def test_forced_decisions_and_modified_migration_warning_survive_restart(rig, forced_skip):
    service, source, calls = rig
    registry = Registry([*service.registry.specs, spec_for("prepare_db")])
    for name in service.registry.registered():
        registry.tool(name)(service.registry.get(name).fn)
    migrations = []

    @registry.tool("prepare_db")
    async def migrate(inp: MigrationInput, ctx: RunContext) -> support.Output:
        migrations.extend(inp.migrations)
        return support.Output()

    service.registry = registry
    p = support.plan()
    p.deploy.local.steps.insert(
        0,
        support.step(
            "deploy.db.local",
            "prepare_db",
            target=Target.LOCAL,
            tier="was",
            params={"migrations": ["0002"]},
            wait_for=["images_ready"],
        ),
    )
    build = p.build.steps[0]
    p.invalidated.append(
        Invalidated(
            id=build.id,
            attempt="include" if forced_skip else "skip",
            result="forced_skip" if forced_skip else "forced_include",
            why="O2 결정적 규칙",
        )
    )
    p.warnings.append(
        PlanWarning(
            code="migration_modified",
            message="적용된 마이그레이션 파일이 바뀌었다: 0001",
        )
    )
    if forced_skip:
        p.build.steps.clear()
        p.build.skipped.append(
            SkippedStep(
                id=build.id,
                tool=build.tool,
                layer=build.layer,
                reason="tree_unchanged",
            )
        )
    rid = service.prepare(p, RunContext(p.run_id, project=p.project), source)
    view = service.approval_view(rid)
    assert view["plan"]["invalidated"][0]["result"] == (
        "forced_skip" if forced_skip else "forced_include"
    )
    assert view["plan"]["warnings"][0]["code"] == "migration_modified"
    service.close()
    restarted = DeploymentService(service.registry, service.root)
    try:
        assert restarted.approval_view(rid) == view
        restarted.approve(rid, approver="operator")
        restarted.start(rid)
        assert (await restarted.wait(rid)).status is RunStatus.SUCCEEDED
        assert sum(name == "build" for name, ctx in calls.contexts) == (0 if forced_skip else 1)
        assert migrations == ["0002"]  # 경고의 기존 0001을 재주입하거나 재적용하지 않는다.
    finally:
        restarted.close()
