"""SQLite 승인 대기 복원과 변경된 입력 차단. 실제 배포/네트워크 없음."""

import json

import pytest

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError
from ddak.executor.engine import RunStatus
from ddak.executor.service import DeploymentService, source_facts
from tests.unit import test_deployment_service as support

rig = support.rig
pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("approved_before", [False, True])
async def test_restart_approval_and_execute(rig, approved_before):
    service, source, calls = rig
    rid = support.prepare(service, source, patch=True)
    expected = service.approval_view(rid)
    if approved_before:
        service.approve(rid, approver="operator")
    service.close()
    reopened = DeploymentService(service.registry, service.root)
    try:
        assert reopened.approval_view(rid) == expected
        assert reopened.get_run(rid)["status"] == (
            "APPROVED" if approved_before else "AWAITING_APPROVAL"
        )
        reopened.approve(rid, approver="operator")
        reopened.start(rid)
        assert (await reopened.wait(rid)).status is RunStatus.SUCCEEDED
        assert any(name == "build" for name, _ in calls.contexts)
        assert (source / "app.py").read_text() == "VERSION = 1\n"
        with pytest.raises(DdakToolError):
            reopened.start(rid)
    finally:
        reopened.close()


@pytest.mark.parametrize("changed", ["plan", "context", "patch", "infra_meta", "source"])
async def test_restart_does_not_reapprove_changed_inputs(rig, changed):
    service, source, calls = rig
    rid = support.prepare(service, source, patch=True)
    service.close()
    directory = service.root / "runs" / rid
    if changed == "plan":
        path = directory / "plan.json"
        data = json.loads(path.read_text())
        data["build"]["steps"][0]["reason"] = "changed after preparation"
        path.write_text(json.dumps(data))
    elif changed == "context":
        with service.store.connection() as db:
            data = service.store.prepared(rid)
            data["context"]["cloud_domain"] = "other.example.test"
            db.execute("UPDATE prepared_runs SET payload=? WHERE run_id=?", (json.dumps(data), rid))
    elif changed == "patch":
        (directory / "approved.patch").write_bytes(
            support.PATCH.replace(b"VERSION = 2", b"VERSION = 3")
        )
    elif changed == "infra_meta":
        (directory / "approval-meta.json").write_text('{"infra_summary": {"summary": "changed"}}')
    else:
        (source / "app.py").write_text("VERSION = 7\n")
    reopened = DeploymentService(service.registry, service.root)
    try:
        if changed == "source":
            reopened.approve(rid, approver="operator")
            reopened.start(rid)
            assert (await reopened.wait(rid)).status is RunStatus.FAILED_BEFORE_DEPLOY
        else:
            with pytest.raises(DdakToolError):
                reopened.approve(rid, approver="operator")
        assert calls.contexts == []
    finally:
        reopened.close()


async def test_custom_facts_reader_requires_explicit_reinjection(rig):
    service, source, _ = rig

    def reader(root):
        return source_facts(root)

    p = support.plan()
    rid = service.prepare(p, RunContext(p.run_id, project=p.project), source, facts_reader=reader)
    key = service.store.prepared(rid)["facts_reader"]
    service.close()
    reopened = DeploymentService(service.registry, service.root)
    try:
        with pytest.raises(DdakToolError, match="facts_reader"):
            reopened.approve(rid, approver="operator")
    finally:
        reopened.close()
    reopened = DeploymentService(service.registry, service.root, facts_readers={key: reader})
    try:
        reopened.approve(rid, approver="operator")
        reopened.start(rid)
        assert (await reopened.wait(rid)).status is RunStatus.SUCCEEDED
    finally:
        reopened.close()
