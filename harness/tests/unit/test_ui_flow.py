"""source=fixture: 전체 홈, 준비 자동 갱신, 진행·결과 동선의 회귀."""

import re
import subprocess
from importlib.metadata import distribution
from pathlib import Path

import pytest

from ddak.executor.service import DeploymentService, source_facts
from tests.unit import test_deployment_service as support
from tests.unit.test_ui_integration_fix10 import client_for, prepare

rig = support.rig


def version(html):
    return re.search(r'data-live-version="([a-f0-9]+)"', html).group(1)


def test_overview_includes_unrun_and_old_projects_and_ignores_default(rig, monkeypatch):
    service, _, _ = rig
    monkeypatch.setenv("DDAK_WATCH_PROJECT", "not-the-home")
    service.save_project_settings("new-project", {}, updated_by="fixture", expected_version=0)
    service.store.preparation_failed("old-run", "old-project", {"detail": "old fixture"})
    for i in range(105):
        service.store.preparation_failed(f"busy-{i}", "busy-project", {"detail": "fixture"})
    with client_for(service) as client:
        home = client.get("/projects")
        assert home.status_code == 200 and "<h1>전체 프로젝트</h1>" in home.text
        for project in ("new-project", "old-project", "busy-project"):
            assert f'href="/?project={project}"' in home.text
        assert "not-the-home" not in home.text
        assert "실행 없음" in home.text
        assert 'href="/runs/old-run/result"' in home.text
        detail = client.get("/?project=old-project")
        assert "<h1>old-project</h1>" in detail.text
        assert 'href="/runs/old-run/result"' in detail.text
        assert 'href="/runs/busy-' not in detail.text
        assert 'class="brand" href="/"' in detail.text
        assert 'action="/ops/plan"' in detail.text and 'action="/ops/unlock"' in detail.text
        ops = client.get("/ops?project=old-project").text
        assert 'href="/runs/old-run/result"' in ops
        assert 'href="/runs/busy-' not in ops


def test_empty_overview_offers_project_connection(rig):
    service, _, _ = rig
    with client_for(service) as client:
        html = client.get("/projects").text
        # 저장된 프로젝트가 없으면 전체 목록에도 다른 화면과 같은 생성 안내 폼이 보인다.
        assert "<h1>전체 프로젝트</h1>" in html
        assert "data-project-required" in html and "프로젝트를 먼저 만드세요" in html
        assert 'method="get" action="/settings"' in html
        assert 'name="project" required' in html and 'pattern="[a-z][a-z0-9_' in html


@pytest.mark.anyio
async def test_automatic_preparation_becomes_visible_then_approval(rig):
    service, source, calls = rig
    await service.begin_preparation("flaskr-three", "auto-first")
    try:
        assert "flaskr-three" in service.list_projects()
        assert service.list_preparations("flaskr-three")[0]["status"] == "PREPARING"
        with client_for(service) as client:
            before = client.get("/?project=flaskr-three").text
            assert "승인 자료 준비 중" in before and 'data-wait-clock="auto-first"' in before
            assert "자동으로 나타납니다" in before and "3초마다" in before
            assert "Terraform 생성 중" not in before
            prepare(service, source, "auto-first")
            service.end_preparation("flaskr-three", "auto-first")
            after = client.get("/?project=flaskr-three").text
            assert version(before) != version(after)
            assert 'href="/runs/auto-first/approval"' in after
            assert "승인 자료 준비 중" not in after
            assert not calls.contexts and not service.get_approvals("auto-first")
    finally:
        service.end_preparation("flaskr-three", "auto-first")


@pytest.mark.parametrize("status", ["FAILED_BEFORE_DEPLOY", "CANCELLED"])
def test_preparation_without_run_has_visible_terminal_reason(rig, status):
    service, _, _ = rig
    service._preparation_requests["prepare-only"] = {
        "project": "plain",
        "request_id": "prepare-only",
        "run_id": None,
        "status": status,
        "detail": "fixture preparation stopped",
    }
    with client_for(service) as client:
        html = client.get("/?project=plain").text
        assert "fixture preparation stopped" in html
        assert 'href="/runs/None/' not in html
        assert "승인 자료 준비 중" not in html
        assert f'data-code="{status}"' in client.get("/projects").text
        ops = client.get("/ops?project=plain").text
        assert "실행 기록 없음" in ops and "data-live-region" in ops


def test_unfinished_result_returns_to_actual_next_action(rig):
    service, source, _ = rig
    rid = prepare(service, source)
    with client_for(service) as client:
        response = client.get(f"/runs/{rid}/result", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"/runs/{rid}/approval"


def test_unrestorable_approval_shows_error_without_redirect_loop(rig):
    service, source, calls = rig

    def reader(root):
        return source_facts(root)

    rid = prepare(service, source, facts_reader=reader)
    service.close()
    reopened = DeploymentService(service.registry, service.root)
    try:
        with client_for(reopened) as client:
            response = client.get(f"/runs/{rid}/approval")
            assert response.status_code == 409 and len(response.history) == 1
            assert "승인 자료를 불러오지 못했습니다" in response.text
            assert "facts_reader" in response.text
            assert "승인하고 배포" not in response.text
            alias = client.get(f"/ops/runs/{rid}/approval")
            assert alias.status_code == 409 and alias.text == response.text
            assert reopened.get_run(rid)["status"] == "AWAITING_APPROVAL"
            assert not reopened.get_approvals(rid) and not calls.contexts
    finally:
        reopened.close()


@pytest.mark.anyio
async def test_old_manual_preparation_failure_does_not_mask_new_success(rig):
    service, source, _ = rig
    requested = service.enqueue_deployment("flaskr-three")
    await service._preparation_tasks[requested["request_id"]]
    failed = service.get_preparation(requested["request_id"])
    assert failed["status"] == "FAILED_BEFORE_DEPLOY" and failed["created"] > 0
    rid = prepare(service, source, "later-auto")
    service.store.finish(rid, "SUCCEEDED", {"tracks": {"local": "DONE", "cloud": "DONE"}}, {}, {})
    with client_for(service) as client:
        home = client.get("/projects").text
        detail = client.get("/?project=flaskr-three").text
        assert 'data-code="SUCCEEDED"' in home
        assert 'data-code="FAILED_BEFORE_DEPLOY"' not in home
        assert failed["detail"] not in detail
        # A later failure still needs attention; only the obsolete one is hidden.
        service._preparation_requests[requested["request_id"]]["created"] = (
            service.get_run(rid)["created"] + 1
        )
        assert 'data-code="FAILED_BEFORE_DEPLOY"' in client.get("/projects").text
        assert failed["detail"] in client.get("/?project=flaskr-three").text


def test_pending_approval_survives_recent_history_limit(rig):
    service, source, calls = rig
    rid = prepare(service, source, "old-approval")
    for i in range(101):
        service.store.preparation_failed(f"later-{i}", "flaskr-three", {"detail": "fixture"})
    with client_for(service) as client:
        for path in ("/projects", "/?project=flaskr-three", "/ops?project=flaskr-three"):
            html = client.get(path).text
            assert f'href="/runs/{rid}/approval"' in html
        assert "검토하고 승인" in client.get("/?project=flaskr-three").text
        assert service.get_run(rid)["status"] == "AWAITING_APPROVAL"
        assert not calls.contexts
        service.approve(rid, approver="fixture")
        home = client.get("/projects").text
        assert 'data-code="APPROVED"' in home
        assert f'href="/runs/{rid}/progress">진행 보기</a>' in home


def test_progress_shows_independent_work_and_stops_busy_for_terminal(rig):
    service, source, _ = rig
    rid = prepare(service, source)
    service.approve(rid, approver="fixture")
    with client_for(service) as client:
        html = client.get(f"/runs/{rid}/progress").text
        for track in ("local", "cloud", "common"):
            assert f'id="{track}-activity"' in html
        assert 'id="event-age"' in html and "승인 완료" in html
        service.store.finish(
            rid, "SUCCEEDED", {"tracks": {"local": "DONE", "cloud": "N/A"}}, {}, {}
        )
        html = client.get(f"/runs/{rid}/progress").text
        assert 'data-activity="ended"' in html
        assert 'data-code="DONE"' in html and 'data-code="N/A"' in html


def test_polling_refresh_retry_and_teardown_without_post():
    script = r"""
    const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
    const code=fs.readFileSync(process.argv[1],'utf8');
    (async()=>{
      const timeouts=new Map(), intervals=new Map(), events={};let id=0,requests=0,replaced=0;
      const status={textContent:''};
      let region={dataset:{liveVersion:'one'},contains:()=>false,
        querySelectorAll:()=>[],replaceWith(next){
        replaced++;region=next}};
      const next={dataset:{liveVersion:'two'},contains:()=>false,
        querySelectorAll:()=>[],replaceWith:region.replaceWith};
      const document={hidden:false,activeElement:null,
        querySelector:s=>s==='[data-live-region]'?region:
          s==='[data-refresh-state]'?status:null,querySelectorAll:()=>[],importNode:n=>n};
      const window={location:{href:'http://127.0.0.1/?project=demo'},
        setTimeout(fn){timeouts.set(++id,fn);return id},
        clearTimeout(n){timeouts.delete(n)},
        setInterval(fn){intervals.set(++id,fn);return id},
        clearInterval(n){intervals.delete(n)},addEventListener(n,fn){events[n]=fn}};
      let error=false;
      const fetch=async(url,opts)=>{
        requests++;assert.equal(url,window.location.href);assert.equal(opts.method,undefined);
        if(error)throw Error('offline');return {ok:true,text:async()=>'<html/>'}};
      class DOMParser{parseFromString(){return {querySelector:()=>next}}}
      vm.runInNewContext(code,{document,window,fetch,DOMParser,AbortController});
      const tick=async()=>{const [key,fn]=[...timeouts][0];timeouts.delete(key);await fn()};
      await tick();
      assert.equal(requests,1);
      assert.equal(replaced,1);
      assert.ok(status.textContent.includes('최신 상태 확인'));
      
      await tick();assert.equal(replaced,1); // stable data keeps focus and DOM
      document.hidden=true;await tick();assert.equal(requests,2);
      document.hidden=false;
      error=true;
      await tick();
      assert.ok(status.textContent.includes('연결이 끊겼습니다'));
      assert.equal(replaced,1);
      
      error=false;await tick();assert.ok(status.textContent.includes('최신 상태 확인'));
      events.pagehide();assert.equal(timeouts.size,0);assert.equal(intervals.size,0);
    })().catch(e=>{console.error(e);process.exitCode=1});
    """
    package = distribution("nodejs-wheel-binaries")
    node = next(package.locate_file(f) for f in package.files or () if str(f).endswith("/bin/node"))
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [str(node), "-e", script, str(root / "src/ddak/web/static/app.js")],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("status", ["gray", "red"])
def test_onboarding_attention_participates_in_dashboard_refresh(rig, status):
    from types import SimpleNamespace

    service, source, _ = rig
    prepare(service, source)
    checklist = [{"label": "연결 검사", "status": status, "detail": "확인이 필요합니다"}]
    checklist.append({"label": "빌드 연결", "status": "green", "detail": "연결됨"})
    service.onboarding = SimpleNamespace(view=lambda _: {"checklist": checklist})
    with client_for(service) as client:
        initial = client.get("/?project=flaskr-three").text
        assert "연결 확인이 필요합니다" in initial and "연결 상태 점검표" not in initial
        assert 'href="/settings?project=flaskr-three#connection-checklist"' in initial
        settings = client.get("/settings?project=flaskr-three").text
        assert "연결 상태 점검표" in settings and "확인이 필요합니다" in settings
        assert ("미확인" if status == "gray" else "오류") in settings
        checklist[0].update(status="green", detail="연결됨")
        updated = client.get("/?project=flaskr-three").text
        assert version(initial) != version(updated)
        assert "연결 확인이 필요합니다" not in updated
        assert "연결 상태 점검표" not in updated and "연결됨" not in updated
        settings = client.get("/settings?project=flaskr-three").text
        assert "연결 확인됨" in settings and "연결됨" in settings


def test_setup_operation_result_retains_onboarding_redirect(rig):
    service, source, _ = rig
    rid = prepare(service, source)
    service.store.finish(rid, "SUCCEEDED", {"setup_operation": "fixture"}, {}, {})
    with client_for(service) as client:
        response = client.get(f"/runs/{rid}/result", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/setup/actions?project=flaskr-three"
