"""통합 승인 복원은 근거를 보존하고 원본 패치를 다시 노출하지 않는다."""

import json
import stat

import pytest

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError
from ddak.executor.service import DeploymentService
from tests.unit import test_deployment_service as support

rig = support.rig


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [False, True])
async def test_restart_approval_preserves_basis_without_raw_patch(rig, legacy):
    service, source, _ = rig
    plan = support.plan(patch=True)
    service.record_stage(plan.run_id, "intake", 7, "succeeded")
    service.prepare(
        plan,
        RunContext(plan.run_id, project=plan.project, toggles=plan.toggles),
        source,
        patch=support.PATCH,
    )
    directory = service.root / "runs" / plan.run_id
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "approved.patch").stat().st_mode) == 0o600
    (directory / "facts.json").write_text(json.dumps({"smoke_groups": ["v2"], "env_keys": []}))
    service.approve(plan.run_id, approver="fixture")
    service.start(plan.run_id)
    await service.wait(plan.run_id)
    # 과거 export에 원문이 남아 있는 경우도 출력 계약은 비노출이다.
    saved = directory / "approval-view.json"
    if legacy:
        saved.unlink()
    else:
        data = json.loads(saved.read_text())
        data["patch"] = support.PATCH.decode()
        saved.write_text(json.dumps(data))
    with service.store.connection() as db:
        db.execute("DELETE FROM prepared_runs WHERE run_id=?", (plan.run_id,))
    registry, root = service.registry, service.root
    await service.shutdown()
    restored = DeploymentService(registry, root)
    try:
        view = restored.approval_view(plan.run_id)
        assert view["patch"] is None
        assert view["decision_basis"]["facts"]["smoke_groups"] == ["v2"]
        assert view["subjects"]["patch"]
    finally:
        await restored.shutdown()


def test_stage_directory_does_not_allow_existing_artifacts_to_be_overwritten(rig):
    service, source, _ = rig
    plan = support.plan()
    service.record_stage(plan.run_id, "intake", 2, "succeeded")
    path = service.root / "runs" / plan.run_id / "plan.json"
    path.write_text("existing artifact")
    with pytest.raises(DdakToolError, match="덮어쓸"):
        service.prepare(plan, RunContext(plan.run_id, project=plan.project), source)
    assert path.read_text() == "existing artifact"
