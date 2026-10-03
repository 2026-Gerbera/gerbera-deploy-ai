"""실제 Store + 가짜 adapter. Docker/VM/네트워크 실행은 없다."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ddak.core.contracts.approval import ApprovalRecord
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.setup_actions import SetupActions
from ddak.core.store import Store
from ddak.web.routes.setup_actions import router

ARGS = {
    "database": "appdb",
    "backup_database": "backupdb",
    "app_account": "appuser",
    "migrator_account": "migrator",
}


@dataclass(frozen=True)
class FakePlan:
    approval_sha: str = "a" * 64
    summary: str = '{"tables":["posts"],"preserve_volume":true}'


class FakeAdapter:
    def __init__(self) -> None:
        self.plans = []
        self.applies = []
        self.failure = None
        self.entered = None
        self.unblock = None
        self.during_apply = None

    def plan(self, kind, args):
        self.plans.append((kind, args))
        return FakePlan()

    def apply(self, plan, approvalcheck):
        assert approvalcheck(plan.approval_sha, plan.summary) is True
        assert approvalcheck("b" * 64, plan.summary) is False
        assert approvalcheck(plan.approval_sha, "changed") is False
        self.applies.append(plan)
        if self.entered:
            self.entered.set()
            assert self.unblock.wait(3)
        if self.during_apply:
            self.during_apply()
        if self.failure:
            raise self.failure


@pytest.fixture
def coordinator(tmp_path: Path):
    store = Store(tmp_path / "state.sqlite")
    service = SimpleNamespace(root=tmp_path, store=store, resolve_project=lambda name: name)
    adapter = FakeAdapter()
    return SetupActions(service, lambda project: adapter), adapter, store, service


def test_plan_public_summary_private_persistence_and_no_apply(coordinator):
    actions, adapter, store, service = coordinator
    public = actions.plan("demo", "database", ARGS)
    assert public["hash"] == "a" * 64 and adapter.applies == []
    assert adapter.plans == [("database", ARGS)]
    path = service.root / "setup/demo/actions" / (public["id"] + ".json")
    assert json.loads(path.read_text()) == public
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert "adapter" not in path.read_text() and "password" not in path.read_text()
    assert store.list_runs() == []
    assert actions.view("demo")["actions"] == [public]


def test_approval_audited_real_lock_and_success_does_not_change_environment(coordinator):
    actions, adapter, store, _service = coordinator
    store.create_run("prior", "demo", "old")
    manifest = {"source_sha": "d" * 40}
    store.finish(
        "prior",
        "SUCCEEDED",
        {},
        manifest,
        {"local": ("SUCCEEDED", manifest), "cloud": ("SUCCEEDED", manifest)},
    )
    before = store.environments("demo")
    public = actions.plan("demo", "database", ARGS)
    result = actions.apply("demo", public["id"], public["hash"], "operator")
    assert result["status"] == "SUCCEEDED" and len(adapter.applies) == 1
    assert store.environments("demo") == before
    run = store.run(public["id"])
    assert run["status"] == "SUCCEEDED" and run["plan_hash"] == "sha256:" + public["hash"]
    assert run["result"]["setup_operation"]["id"] == public["id"]
    record = store.approvals(public["id"])[0]
    assert record.approver == "operator" and record.bound_to == "sha256:" + public["hash"]
    assert record.kind == "deploy" and record.decision == "approved"
    assert store.release_record(public["id"]) == {}
    store.assert_idle("demo")
    with pytest.raises(DdakToolError):
        actions.apply("demo", public["id"], public["hash"], "operator")
    assert len(adapter.applies) == 1


def test_wrong_hash_project_and_actor_never_apply(coordinator):
    actions, adapter, store, _service = coordinator
    public = actions.plan("demo", "database", ARGS)
    for project, sha, actor in [
        ("demo", "0" * 64, "operator"),
        ("other", public["hash"], "operator"),
        ("demo", public["hash"], ""),
    ]:
        with pytest.raises(DdakToolError):
            actions.apply(project, public["id"], sha, actor)
    assert store.list_runs() == [] and adapter.applies == []


def test_new_plan_supersedes_old_and_restart_requires_replan(coordinator):
    actions, adapter, _store, service = coordinator
    first = actions.plan("demo", "database", ARGS)
    second = actions.plan("demo", "ownership", {"tier": "was", "replica": 2})
    with pytest.raises(DdakToolError):
        actions.apply("demo", first["id"], first["hash"], "operator")
    restarted = SetupActions(service, lambda project: adapter)
    view = restarted.view("demo")["actions"]
    assert any(
        entry["id"] == second["id"] and entry["status"] == "REPLAN_REQUIRED" for entry in view
    )
    with pytest.raises(DdakToolError, match="재계획"):
        restarted.apply("demo", second["id"], second["hash"], "operator")
    assert not adapter.applies


@pytest.mark.parametrize(
    "kind,args",
    [
        ("ownership", {"tier": "db", "replica": 1}),
        ("ownership", {"tier": "was", "replica": True}),
        ("ownership", {"tier": "web", "replica": 0}),
        ("database", {**ARGS, "password": "fake-value"}),
        ("database", {**ARGS, "database": "bad;sql"}),
        ("database", {**ARGS, "backup_database": "appdb"}),
        ("unknown", {}),
    ],
)
def test_invalid_inputs_and_immutable_db_owner_rejected_before_factory(coordinator, kind, args):
    actions, adapter, store, _service = coordinator
    with pytest.raises(DdakToolError) as error:
        actions.plan("demo", kind, args)
    if args.get("tier") == "db":
        assert "NEEDS_CONTEXT" in str(error.value)
    assert not adapter.plans and not adapter.applies and store.list_runs() == []


def approved_pipeline(store, project="demo"):
    ident = "run-20261003-010101-dead"
    store.create_run(ident, project, "sha256:" + "e" * 64)
    store.approve(
        [
            ApprovalRecord(
                run_id=ident,
                project=project,
                approval_id="pipeline",
                kind="deploy",
                bound_to="sha256:" + "e" * 64,
                approver="operator",
                approved_at=datetime.now(UTC),
                decision="approved",
            )
        ]
    )
    return ident


def test_pipeline_project_lock_blocks_setup_plan_and_apply(coordinator):
    actions, adapter, store, _service = coordinator
    public = actions.plan("demo", "database", ARGS)
    ident = approved_pipeline(store)
    token = store.acquire("demo", ident, targets="local")
    with pytest.raises(DdakToolError):
        actions.plan("demo", "ownership", {"tier": "web", "replica": 1})
    with pytest.raises(DdakToolError):
        actions.apply("demo", public["id"], public["hash"], "operator")
    assert not adapter.applies
    store.mark_stopped(ident, "CANCELLED")
    store.release("demo", ident, token)


def test_setup_holds_pipeline_lock_and_duplicate_submit_fails(coordinator):
    actions, adapter, store, _service = coordinator
    public = actions.plan("demo", "ownership", {"tier": "web", "replica": 1})
    adapter.entered, adapter.unblock = threading.Event(), threading.Event()
    ident = approved_pipeline(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(actions.apply, "demo", public["id"], public["hash"], "operator")
        assert adapter.entered.wait(2)
        with pytest.raises(DdakToolError):
            store.acquire("demo", ident, targets="local")
        with pytest.raises(DdakToolError):
            actions.apply("demo", public["id"], public["hash"], "operator")
        adapter.unblock.set()
        assert running.result(timeout=3)["status"] == "SUCCEEDED"
    assert len(adapter.applies) == 1


@pytest.mark.parametrize("uncertain", [True, False])
def test_manager_failure_stops_and_uncertain_blocks_only_local(coordinator, uncertain):
    actions, adapter, store, _service = coordinator
    manifest = {"source_sha": "d" * 40}
    store.create_run("prior", "demo", "old")
    store.finish(
        "prior",
        "SUCCEEDED",
        {},
        manifest,
        {"local": ("SUCCEEDED", manifest), "cloud": ("SUCCEEDED", manifest)},
    )
    before = store.environments("demo")
    failure = DdakToolError(ErrorCode.PRECONDITION_FAILED, "manager password=" + "fake-private")
    failure.needs_human = uncertain
    adapter.failure = failure
    public = actions.plan("demo", "database", ARGS)
    with pytest.raises(DdakToolError) as error:
        actions.apply("demo", public["id"], public["hash"], "operator")
    status = "NEEDS_HUMAN" if uncertain else "CANCELLED"
    assert store.run(public["id"])["status"] == status
    assert error.value.needs_human is uncertain
    assert "fake-private" not in str(error.value)
    environments = store.environments("demo")
    assert environments["local"]["current"] == before["local"]["current"]
    assert environments["cloud"] == before["cloud"]
    if uncertain:
        assert environments["local"]["status"] == "NEEDS_HUMAN"
        ident = approved_pipeline(store)
        with pytest.raises(DdakToolError):
            store.acquire("demo", ident, targets="local")
    else:
        assert environments == before


def test_unclassified_failure_is_human_and_no_raw_output(coordinator):
    actions, adapter, store, _service = coordinator
    adapter.failure = RuntimeError("fake-" + "private-output")
    public = actions.plan("demo", "database", ARGS)
    with pytest.raises(DdakToolError) as error:
        actions.apply("demo", public["id"], public["hash"], "operator")
    assert error.value.needs_human is True
    assert "fake-private-output" not in str(error.value)
    assert store.run(public["id"])["status"] == "NEEDS_HUMAN"


def test_lock_or_approval_loss_after_adapter_prevents_success(coordinator):
    actions, adapter, store, _service = coordinator
    public = actions.plan("demo", "database", ARGS)
    adapter.during_apply = lambda: store.mark_stopped(public["id"], "NEEDS_HUMAN")
    with pytest.raises(DdakToolError):
        actions.apply("demo", public["id"], public["hash"], "operator")
    assert store.run(public["id"])["status"] == "NEEDS_HUMAN"


@pytest.fixture
def panel(coordinator):
    actions, adapter, store, service = coordinator
    service.setup_actions = actions
    app = FastAPI()
    app.state.settings = SimpleNamespace(admin_port=8765)
    app.state.deployment = service
    app.include_router(router)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/setup/actions?project=demo").status_code == 200
        yield client, actions, adapter, store


def post(client, route, data, **kwargs):
    return client.post(
        "/setup/actions/" + route,
        data={"project": "demo", "csrf_token": client.cookies["ddak_csrf"], **data},
        headers=kwargs.pop("headers", {"Origin": "http://127.0.0.1:8765"}),
        **kwargs,
    )


def test_ui_one_screen_names_summary_hash_approval_no_password(panel):
    client, actions, adapter, store = panel
    response = post(client, "plan", {"kind": "database", **ARGS})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    html = response.text
    for name in ARGS:
        assert f'name="{name}"' in html
    assert 'name="tier"' in html and 'name="replica"' in html
    assert 'type="password"' not in html
    assert 'name="approved_hash"' in html and "a" * 64 in html and "posts" in html
    assert not adapter.applies
    public = actions.view("demo")["actions"][-1]
    response = post(client, "apply", {"id": public["id"], "approved_hash": public["hash"]})
    assert response.status_code == 200 and "SUCCEEDED" in response.text
    assert store.approvals(public["id"])[0].approver == "local-operator"
    assert len(adapter.applies) == 1


@pytest.mark.parametrize("route", ["plan", "apply"])
@pytest.mark.parametrize("failure", ["token", "origin", "host"])
def test_plan_and_approval_csrf_host_origin_checked_before_callback(panel, route, failure):
    client, _actions, adapter, _store = panel
    data = (
        {"kind": "database", **ARGS} if route == "plan" else {"id": "id", "approved_hash": "hash"}
    )
    headers = {"Origin": "http://127.0.0.1:8765"}
    if failure == "token":
        data["csrf_token"] = "invalid"
    elif failure == "origin":
        headers = {}
    else:
        headers["Host"] = "external.example:8765"
    response = post(client, route, data, headers=headers)
    assert response.status_code in {400, 403}
    assert not adapter.plans and not adapter.applies


def test_manager_needs_context_ui_generic_redacted_escaped(panel):
    client, _actions, adapter, _store = panel
    response = post(client, "plan", {"kind": "ownership", "tier": "db", "replica": "1"})
    assert response.status_code == 409 and "NEEDS_CONTEXT" in response.text
    assert not adapter.plans
    assert post(client, "plan", {"kind": "database", **ARGS, "password": "fake"}).status_code == 400
    assert not adapter.plans


def test_restarted_ui_cannot_approve_and_get_never_executes(panel, coordinator):
    client, actions, adapter, _store = panel
    public = actions.plan("demo", "database", ARGS)
    service = coordinator[-1]
    service.setup_actions = SetupActions(service, lambda project: adapter)
    response = client.get("/setup/actions?project=demo")
    assert "REPLAN_REQUIRED" in response.text and 'name="approved_hash"' not in response.text
    assert (
        post(client, "apply", {"id": public["id"], "approved_hash": public["hash"]}).status_code
        == 409
    )
    assert not adapter.applies


def test_failed_heartbeat_blocks_success_and_requires_human(coordinator, monkeypatch):
    from ddak.core import setup_actions as module

    actions, adapter, store, _service = coordinator
    public = actions.plan("demo", "database", ARGS)
    ticks = []
    failed = threading.Event()
    heartbeat = store.heartbeat

    def heartbeat_failure(project, ident, token):
        ticks.append(ident)
        if len(ticks) > 1:
            failed.set()
            raise DdakToolError(ErrorCode.LOCK_INVALID, "fake lease failure")
        return heartbeat(project, ident, token)

    monkeypatch.setattr(module, "_HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(store, "heartbeat", heartbeat_failure)
    adapter.during_apply = lambda: failed.wait(2)
    with pytest.raises(DdakToolError):
        actions.apply("demo", public["id"], public["hash"], "operator")
    assert len(ticks) >= 2 and store.run(public["id"])["status"] == "NEEDS_HUMAN"
    assert store.environments("demo")["local"]["status"] == "NEEDS_HUMAN"


def test_pending_model_changed_after_plan_is_not_approved(coordinator):
    actions, adapter, store, _service = coordinator
    public = actions.plan("demo", "database", ARGS)
    actions._pending["demo", public["id"]].model = FakePlan(summary="changed")
    with pytest.raises(DdakToolError):
        actions.apply("demo", public["id"], public["hash"], "operator")
    assert not adapter.applies and store.list_runs() == []


def test_setup_restart_recovered_needs_human_overrides_stale_public_status(coordinator):
    actions, adapter, store, service = coordinator
    public = actions.plan("demo", "database", ARGS)
    store.create_run(public["id"], "demo", "sha256:" + public["hash"])
    store.approve(
        [
            ApprovalRecord(
                run_id=public["id"],
                project="demo",
                approval_id="setup",
                kind="deploy",
                bound_to="sha256:" + public["hash"],
                approver="operator",
                approved_at=datetime.now(UTC),
                decision="approved",
            )
        ]
    )
    store.acquire("demo", public["id"], targets="local")
    store.recover_interrupted()
    restarted = SetupActions(service, lambda project: adapter)
    entry = restarted.view("demo")["actions"][0]
    assert entry["status"] == "NEEDS_HUMAN" and "사람" in entry["detail"]
    with pytest.raises(DdakToolError):
        restarted.plan("demo", "database", ARGS)
    assert not adapter.applies


def test_summary_and_redacted_error_are_html_escaped(panel, monkeypatch):
    client, _actions, adapter, _store = panel
    monkeypatch.setattr(adapter, "plan", lambda *args: FakePlan(summary="<script>fake</script>"))
    response = post(client, "plan", {"kind": "database", **ARGS})
    assert response.status_code == 200
    assert "<script>fake</script>" not in response.text
    assert "&lt;script&gt;fake&lt;/script&gt;" in response.text
    public = client.app.state.deployment.setup_actions.view("demo")["actions"][0]
    adapter.failure = DdakToolError(ErrorCode.PRECONDITION_FAILED, "password=" + "fake-value")
    response = post(client, "apply", {"id": public["id"], "approved_hash": public["hash"]})
    assert response.status_code == 409 and "fake-value" not in response.text
