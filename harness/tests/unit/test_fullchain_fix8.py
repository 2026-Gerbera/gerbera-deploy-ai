"""source=fixture: 온프렘 준비 실패·독립 트랙·운영 연결 회귀."""

import shutil
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI, HTTPException, Request

from ddak import app
from ddak.cd.fake import FakeProvider
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.enums import RunMode, Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.analyze_project import AnalyzeProjectInput
from ddak.core.contracts.tools.validate_plan import ValidatePlanInput
from ddak.core.runtime import tool_context
from ddak.executor.engine import RunStatus, TrackStatus
from ddak.plan.analyze import analyze_project
from ddak.plan.validate import validate_plan
from ddak.web.routes.ops import approval_page, operate, page
from tests.unit import test_deployment_service as service_support
from tests.unit.onprem.deploy import test_onprem_vm as vm_support
from tests.unit.plan.analyze.test_analyze import FakeJev
from tests.unit.plan.validate.test_validate import facts
from tests.unit.test_deployment_service import plan, step

vm = vm_support.vm
rig = service_support.rig


@pytest.mark.anyio
async def test_missing_cloud_tool_fails_only_cloud_and_persists_local_success(rig):
    service, source, calls = rig
    p = plan()
    p.deploy.cloud.steps.insert(
        0,
        step(
            "deploy.secrets.cloud",
            "sync_env_to_cloud",
            target=Target.CLOUD,
            params={"keys": ["SECRET_KEY"]},
        ),
    )
    rid = service.prepare(p, RunContext(p.run_id, project=p.project), source)
    assert service.approval_view(rid)["preparation_failures"] == {"cloud": ["sync_env_to_cloud"]}
    service.approve(rid, approver="fixture")
    service.start(rid)
    result = await service.wait(rid)
    assert result.status is RunStatus.FAILED_CLOUD
    assert result.tracks["local"] is TrackStatus.DONE
    assert result.tracks["cloud"] is TrackStatus.FAILED
    assert all("cloud" not in n and n != "compare" for n, _ in calls.contexts)
    assert service.get_environments("demo")["local"]["status"] == "SUCCEEDED"
    assert any(r.tool == "sync_env_to_cloud" and "미등록" in r.error for r in result.records)


def test_local_build_preflight_happens_before_approval(rig):
    service, source, _ = rig
    p = plan()
    ctx = RunContext(
        p.run_id, project=p.project, build_backend="local", image_repository="2026gerbera/flaskr"
    )
    with pytest.raises(DdakToolError, match="사전 점검"):
        service.prepare(p, ctx, source)
    service.build_preflight = lambda _: (_ for _ in ()).throw(
        DdakToolError(ErrorCode.PRECONDITION_FAILED, "builder missing")
    )
    with pytest.raises(DdakToolError, match="builder missing"):
        service.prepare(p, ctx, source)
    assert service.list_runs() == []


@pytest.mark.anyio
async def test_local_build_login_warning_reaches_persisted_approval_page(rig, monkeypatch):
    from ddak.cloud.build import preflight_local_build
    from tests.unit.cloud.build.test_local import FakeRunner

    service, source, _ = rig
    runner = FakeRunner()
    runner.info = "Client:\nServer:\n"
    monkeypatch.setattr(
        app, "preflight_local_build", lambda repo: preflight_local_build(repo, runner=runner)
    )
    # app adapter의 REAL 분기만 검증하고 실제 배포 인벤토리는 이 준비에서 요구하지 않는다.
    ctx = RunContext(
        "run-warning", project="demo", build_backend="local", image_repository="test-team/app"
    )
    service.build_preflight = lambda c: app._local_build_preflight(
        replace(c, adapter_mode=app.AdapterMode.REAL)
    )
    p = plan(ctx.run_id)
    rid = service.prepare(p, ctx, source)
    expected = preflight_local_build(ctx.image_repository, runner=runner)
    assert service.get_run(rid)["status"] == "AWAITING_APPROVAL"
    assert service.approval_view(rid)["preparation_warnings"] == expected
    service.close()
    reopened = service_support.DeploymentService(service.registry, service.root)
    assert reopened.approval_view(rid)["preparation_warnings"] == expected
    application = FastAPI()
    application.state.settings = Settings()
    application.state.deployment = reopened
    request = Request(
        {"type": "http", "method": "GET", "path": "/ops", "app": application, "headers": []}
    )
    response = await approval_page(request, rid)
    assert response.status_code == 200
    assert "준비 경고" in response.body.decode() and expected[0] in response.body.decode()
    try:
        reopened.approve(rid, approver="fixture")
    finally:
        reopened.close()


def test_tempbox_analyze_assemble_runtime_config_with_fake_ssh(tmp_path, vm):
    _fake, provider, ctx = vm
    src = tmp_path / "source"
    original = Path(__file__).parents[2] / "apps/temp-box/flaskr"
    shutil.copytree(original, src / "flaskr")
    # 키 이름만의 새 템플릿: 앱의 비밀/개발값을 읽어 복제하지 않는다.
    (src / "env.example").write_text(
        "DATABASE_URL=\nSECRET_KEY=\nAPP_BASE_URL=\nDATABASE_URL_MIGRATOR=\nRELEASE_ID=\nSOURCE_SHA=\n"
    )
    cfg = {"tiers": {"was": {"paths": ["flaskr"]}}, "env_example": "env.example"}
    ctx = replace(
        ctx, mode=RunMode.BOOTSTRAP, deploy_config=cfg, source_sha="a" * 40, candidate_sha="b" * 40
    )
    inp = AnalyzeProjectInput(
        run_id=ctx.run_id,
        request=DeployRequest(
            project=ctx.project, repo_url="https://github.com/fixture/app", target="local"
        ),
        source_dir="source",
        changed={"local": {"was": True}},
        changed_paths=(),
    )
    with tool_context("analyze_project", ctx.run_id):
        analyzed = analyze_project(inp, ctx, root=tmp_path, jev_client=FakeJev(0.9))
    assert "DATABASE_URL_MIGRATOR" not in {k.name for k in analyzed.env_keys}
    assert all(k.is_new for k in analyzed.env_keys if k.name in {"DATABASE_URL", "SECRET_KEY"})
    f = facts(
        project=ctx.project,
        mode=RunMode.BOOTSTRAP,
        target="local",
        tiers=("was",),
        env_keys=analyzed.env_keys,
        changed={"local": {"was": True}},
        new_migrations=(),
        db_initialized={"local": False},
    )
    p = validate_plan(ValidatePlanInput(run_id=ctx.run_id, facts=f), ctx)
    inject = next(s for s in p.deploy.local.steps if s.tool == "inject_env_config")
    tier = ctx.platform["onprem"]["tiers"]["was"]
    keys = set(inject.params["keys"])
    private = Path(tier["env_file"])
    private.write_text(
        "\n".join(k + "=fixture-value" for k in keys - {"SECRET_KEY", "RELEASE_ID", "SOURCE_SHA"})
        + "\n"
    )
    private.chmod(0o600)
    provider.inject_config(inject.params["keys"], ctx)
    assert "DATABASE_URL_MIGRATOR" not in inject.params["keys"]


@pytest.mark.parametrize("key", ["DATABASE_URL_MIGRATOR", "PASSWORD_MIGRATOR", "db_migrator"])
def test_fake_runtime_rejects_same_migration_keys(key):
    with pytest.raises(DdakToolError, match="마이그레이션"):
        FakeProvider(Target.LOCAL).inject_config([key], RunContext("run-1"))


@pytest.mark.anyio
async def test_ops_csrf_and_manual_plan_endpoint(rig):
    service, _source, _calls = rig
    application = FastAPI()
    application.state.settings = Settings()
    application.state.deployment = service
    token = "f" * 40

    def request(method="GET", data=None, safe=True):
        async def receive():
            return {"type": "http.request", "body": urlencode(data or {}).encode()}

        headers = [
            (b"host", b"127.0.0.1:8765"),
            (b"content-type", b"application/x-www-form-urlencoded"),
        ]
        if safe:
            headers += [
                (b"origin", b"http://127.0.0.1:8765"),
                (b"cookie", ("ddak_csrf=" + token).encode()),
            ]
        return Request(
            {
                "type": "http",
                "method": method,
                "path": "/ops",
                "app": application,
                "headers": headers,
            },
            receive,
        )

    response = await page(request())
    assert response.status_code == 200 and "지금 계획 요청" in response.body.decode()
    with pytest.raises(HTTPException) as exc:
        await operate(request("POST", {"project": "demo"}, safe=False), "unlock")
    assert exc.value.status_code == 403
    result = await operate(
        request("POST", {"project": "demo", "csrf_token": token, "reason": "fixture checked"}),
        "unlock",
    )
    assert isinstance(result, dict)


def test_saved_settings_take_priority_and_demo_alias_preserved(rig, monkeypatch):
    service, _, _ = rig
    service.save_project_settings(
        "demo",
        {
            "repo_url": "https://github.com/fixture/app",
            "default_targets": "onprem",
            "auto_detect": True,
        },
        updated_by="fixture",
        expected_version=0,
    )
    monkeypatch.setenv("DDAK_WATCH_REPO_URL", "https://github.com/other/app")
    monkeypatch.setenv("DDAK_WATCH_TARGETS", "both")
    assert service.resolve_project("flaskr") == "demo"
    targets = app._watch_targets(service)
    assert len(targets) == 1 and targets[0].project == "demo" and targets[0].target == "local"
    assert targets[0].repo_url == "https://github.com/fixture/app"
