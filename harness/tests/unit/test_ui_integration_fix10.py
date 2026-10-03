"""source=fixture: 합쳐진 관리 UI의 실제 HTTP 승인/실행 동선. 외부 I/O 없음."""

import pytest
from fastapi.testclient import TestClient

from ddak import app as assembly
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.infra_outputs import PLATFORM_OUTPUTS, checked_outputs
from ddak.web.app import create_app
from ddak.web.dependencies import public_links
from ddak.web.routes.ops import router as ops_router
from tests.unit import test_deployment_service as support
from tests.unit.test_approval_meta import HASH, summary

rig = support.rig
BASE = "http://127.0.0.1:8765"
PUBLIC = "https://fixture.trycloudflare.com"
PROJECT = "flaskr-three"


def client_for(service):
    app = create_app(deployment_factory=lambda: service, settings=Settings())
    app.include_router(ops_router)
    return TestClient(app, base_url=BASE)


def post(client, path, **data):
    return client.post(
        path,
        data={"csrf_token": client.cookies.get("ddak_csrf"), **data},
        headers={"origin": BASE, "accept": "text/html"},
        follow_redirects=False,
    )


def prepare(service, source, rid="run-ui", **kwargs):
    plan = support.plan(rid).model_copy(update={"project": PROJECT})
    context = RunContext(
        rid,
        project=PROJECT,
        source_sha="a" * 40,
        targets="both",
        platform={"onprem": {"public_url": PUBLIC}},
        cloud_domain="app.example.com",
        preparation_warnings=["Docker Hub 로그인 확인 불가 — fixture"],
    )
    return service.prepare(plan, context, source, **kwargs)


@pytest.mark.parametrize("failure", [False, True])
def test_dashboard_approval_progress_result_http_flow(rig, monkeypatch, failure):
    service, source, calls = rig
    monkeypatch.setenv("DDAK_WATCH_PROJECT", PROJECT)
    rid = prepare(service, source, subjects={"infra": HASH}, infra_summary=summary())
    calls.fail_cloud = failure
    calls.pause_build = True
    with client_for(service) as client:
        home = client.get("/")
        assert home.status_code == 200 and PROJECT in home.text
        assert f'href="/runs/{rid}/approval"' in home.text
        assert 'action="/ops/plan"' in home.text and 'action="/ops/unlock"' in home.text
        assert f'href="{PUBLIC}"' in home.text and f'href="{PUBLIC}/version"' in home.text
        assert "https://app.example.com/version" in home.text
        approval = client.get(f"/runs/{rid}/approval")
        assert approval.status_code == 200
        for visible in ("a" * 40, "deploy.was.local", "Docker Hub 로그인 확인 불가", HASH):
            assert visible in approval.text
        alias = client.get(f"/ops/runs/{rid}/approval")
        assert alias.status_code == 200 and alias.text == approval.text
        response = post(client, f"/runs/{rid}/approval", decision="approved")
        assert response.status_code == 303
        assert response.headers["location"] == f"/runs/{rid}/progress"
        progress = client.get(response.headers["location"])
        assert progress.status_code == 200
        assert 'data-activity="running"' in progress.text
        assert 'class="activity-dots"' in progress.text
        assert "상세 로그가 도착하면" in progress.text
        assert "FAILED_VERIFY" in progress.text and "SUPERSEDED" in progress.text
        assert f'href="/runs/{rid}/progress"' in client.get("/").text
        assert client.portal is not None
        client.portal.call(calls.resume_build.set)
        client.portal.call(service.wait, rid)
        result = client.get(f"/runs/{rid}/result")
        assert result.status_code == 200
        assert f"{PUBLIC}/version" in result.text
        if failure:
            assert "FAILED_CLOUD" in result.text and "injected cloud failure" in result.text
            assert "승인 기록과 배포 결과를 저장했습니다." not in result.text
        else:
            assert (
                "SUCCEEDED" in result.text
                and "승인 기록과 배포 결과를 저장했습니다." in result.text
            )
        assert f'href="/runs/{rid}/result"' in client.get("/").text
        assert (
            'data-status="' + service.get_run(rid)["status"]
            in client.get(f"/runs/{rid}/progress").text
        )
        assert 'data-activity="ended"' in client.get(f"/runs/{rid}/progress").text
        records = service.get_approvals(rid)
        assert {r.kind for r in records} == {"deploy", "infra"}
        assert next(r.bound_to for r in records if r.kind == "infra") == HASH


def test_failed_preparation_redirects_and_redacts_diagnostics(rig, monkeypatch):
    service, _, _ = rig
    monkeypatch.setenv("DDAK_WATCH_PROJECT", PROJECT)
    rid = "run-ui-prep-fail"
    ctx = RunContext(rid, project=PROJECT, targets="onprem")
    service.record_preparation_failure(
        rid,
        PROJECT,
        DdakToolError(ErrorCode.CONFIG_INVALID, "missing_tool build_image"),
        context=ctx,
        phase="prepare",
    )
    # Fixture-only persisted diagnostic proves rendering does not leak or execute text.
    sentinel = "private-" + "fixture-value"
    service.store.finish(
        rid,
        "FAILED_BEFORE_DEPLOY",
        {
            "phase": "prepare",
            "code": "CONFIG_INVALID",
            "detail": "password=" + sentinel + " <script>bad()</script>",
            "missing_tool": "build_image",
            "infra_changes": [{"status": "applied"}],
        },
        {},
        {},
    )
    with client_for(service) as client:
        response = client.get(f"/runs/{rid}/approval", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].endswith("/result")
        result = client.get(response.headers["location"])
        for word in ("phase", "prepare", "CONFIG_INVALID", "missing_tool", "build_image"):
            assert word in result.text
        assert sentinel not in result.text and "<script>bad()" not in result.text
        assert "[REDACTED]" in result.text and "&lt;script&gt;" in result.text
        assert "인프라 변경 후 실행이 실패했습니다" in result.text
        assert (
            "인프라 변경 없음" not in result.text
            and "승인 기록과 배포 결과를 저장했습니다." not in result.text
        )


def test_superseded_is_terminal_and_approval_refused(rig, monkeypatch):
    service, source, _ = rig
    monkeypatch.setenv("DDAK_WATCH_PROJECT", PROJECT)
    for i in (1, 2):
        rid = f"run-super-{i}"
        plan = support.plan(rid).model_copy(update={"project": PROJECT})
        service.prepare(
            plan,
            RunContext(rid, project=PROJECT, trigger="auto", ref="prod", source_sha=str(i) * 40),
            source,
        )
    with client_for(service) as client:
        home = client.get("/")
        assert "대체됨" in home.text and 'href="/runs/run-super-1/result"' in home.text
        response = post(client, "/runs/run-super-1/approval", decision="approved")
        assert response.status_code == 409 and "SUPERSEDED" in response.text
        events = client.get("/runs/run-super-1/events")
        assert events.status_code == 200 and '"status":"SUPERSEDED"' in events.text
        assert (
            "승인 기록과 배포 결과를 저장했습니다."
            not in client.get("/runs/run-super-1/result").text
        )


def test_watch_project_settings_ops_alias_and_unlock_http(rig, monkeypatch):
    service, _, _ = rig
    monkeypatch.setenv("DDAK_WATCH_PROJECT", PROJECT)
    with client_for(service) as client:
        for path in ("/", "/settings", "/ops"):
            response = client.get(path)
            assert response.status_code == 200 and PROJECT in response.text
        response = post(client, "/ops/unlock", reason="fixture verified")
        assert response.status_code in (200, 303)
        history = service.store.unlock_history(PROJECT)
        assert history[0]["reason"] == "fixture verified"
        denied = client.post("/ops/unlock", data={"project": PROJECT})
        assert denied.status_code in (400, 403)


@pytest.mark.parametrize(
    "raw",
    [
        "javascript:alert(1)",
        "https://name:pass@example.com",
        "https://example.com?token=x",
        "https://example.com\n",
    ],
)
def test_public_urls_never_include_credentials_or_non_http(raw):
    assert public_links({"targets": "onprem", "public_url": raw}) == []


def test_cloud_outputs_deduplicated_and_kept():
    keys = (
        "task_execution_role_arn",
        "task_role_arn",
        "dbinit_execution_role_arn",
        "app_secret_arn_SECRET_KEY",
    )
    assert all(name in PLATFORM_OUTPUTS for name in keys)
    values = {name: "arn:aws:iam::123456789012:role/fixture" for name in keys}
    assert checked_outputs(values, "platform") == values


def test_merge_assembly_keeps_both_refresh_and_tool_registrations(monkeypatch):
    calls = []
    ctx = RunContext("fixture")
    monkeypatch.setattr(assembly, "refresh_infra_context", lambda *args: ctx)
    monkeypatch.setattr(assembly, "seed_registry_secrets", lambda updated: calls.append(updated))
    step = support.step("deploy.infra.cloud", "apply_infra")
    assert assembly._refresh_cloud_context(step, {"layer": "platform"}, ctx) is ctx
    assert calls == [ctx]
    registry = assembly.load_tools()
    assert "sync_env_to_cloud" in registry.registered()
    assert "generate_infra" in registry.registered()


def test_progress_js_uses_server_terminal_states_and_ignores_stepless_events():
    import subprocess
    from importlib.metadata import distribution
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    script = r"""
    const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert');
    let activations = 0, closed = false, visible = false;
    const listeners = {};
    const list = { querySelector: () => null, appendChild: () => {} };
    class EventSource {
      addEventListener(name, fn) { listeners[name] = fn; }
      close() { closed = true; }
    }
    const document = {
      querySelector: (selector) => selector === '[data-run-id]'
        ? { dataset: {runId:'fixture', status:'RUNNING',
          terminalStates: JSON.stringify(['SUCCEEDED','FAILED_VERIFY','SUPERSEDED'])}}
        : null,
      querySelectorAll: (selector) => {
        if (selector === '[data-phase]') activations++;
        return [];
      },
      createElement: () => ({}),
      getElementById: (id) => id === 'result-link'
        ? {classList:{remove:()=>{visible=true;}}} : list,
    };
    vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {document, EventSource});
    listeners['step.started']({data:JSON.stringify({type:'step.started',step:'build.was'})});
    assert.equal(activations,1);
    listeners['gate.opened']({data:JSON.stringify({type:'gate.opened'})});
    assert.equal(activations,1);
    listeners['run.state']({data:JSON.stringify({type:'run.state',status:'FAILED_VERIFY'})});
    assert.equal(activations,1);
    assert.ok(closed && visible);
    """
    # pyright[nodejs]가 이미 공급하는 런타임. PATH의 gitleaks 유무와 무관하다.
    package = distribution("nodejs-wheel-binaries")
    node = next(package.locate_file(f) for f in package.files or () if str(f).endswith("/bin/node"))
    subprocess.run(
        [str(node), "-e", script, str(root / "src/ddak/web/static/app.js")],
        check=True,
        capture_output=True,
        timeout=10,
    )


def test_patch_approval_binds_private_diff_without_showing_source_lines(rig):
    service, source, _ = rig
    plan = support.plan("run-ui-patch", patch=True)
    service.prepare(
        plan,
        RunContext(plan.run_id, project=plan.project, toggles=plan.toggles),
        source,
        patch=support.PATCH,
        patch_meta=support.patch_metadata(),
    )
    with client_for(service) as client:
        response = client.get(f"/runs/{plan.run_id}/approval")
        assert response.status_code == 200
        assert "+VERSION = 2" not in response.text
        assert service.approval_view(plan.run_id)["subjects"]["patch"] in response.text
        assert "approved-patch.diff" not in response.text
        assert client.get(f"/runs/{plan.run_id}/approved-patch.diff").status_code == 404


def test_explicit_project_survives_settings_back_link(rig, monkeypatch):
    service, _, _ = rig
    monkeypatch.setenv("DDAK_WATCH_PROJECT", PROJECT)
    with client_for(service) as client:
        response = client.get("/settings?project=other")
        assert 'href="/?project=other">대시보드' in response.text
        assert "<h1>other</h1>" in client.get("/?project=other").text
