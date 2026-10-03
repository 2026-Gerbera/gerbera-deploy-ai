"""source=fixture: 원장 UI의 환경 분리와 기존 POST 경계 회귀."""

import json
import subprocess
from html.parser import HTMLParser
from importlib.metadata import distribution
from pathlib import Path

import pytest

from ddak.web.dependencies import templates
from ddak.web.narrative import ALIASES, TEXT
from ddak.web.story import environment_cards, result_story
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_ui_integration_fix10 import client_for, post, prepare


class Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"attrs": attrs, "fields": set()}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None:
            self.current["fields"].add(attrs.get("name"))

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


def test_all_post_forms_keep_project_and_csrf_and_denial(rig):
    service, source, _ = rig
    rid = prepare(service, source)
    with client_for(service) as client:
        for path in (
            "/?project=flaskr-three",
            "/settings?project=flaskr-three",
            "/ops?project=flaskr-three",
            f"/runs/{rid}/approval",
        ):
            response = client.get(path)
            assert response.status_code == 200
            parser = Forms()
            parser.feed(response.text)
            assert parser.forms
            for form in parser.forms:
                if form["attrs"].get("method") == "post":
                    assert {"csrf_token", "project"} <= form["fields"]
        response = post(client, f"/runs/{rid}/approval", decision="denied")
        assert response.status_code == 303
        assert response.headers["location"].endswith("/result")
        assert service.get_run(rid)["status"] == "FAILED_BEFORE_DEPLOY"
        assert (
            "승인 기록과 배포 결과를 저장했습니다."
            not in client.get(response.headers["location"]).text
        )


@pytest.mark.parametrize(
    "status,cloud",
    [
        ("FAILED_CLOUD", "FAILED"),
        ("NEEDS_HUMAN", "ROLLBACK_FAILED"),
        ("PARITY_FAILED", "ROLLED_BACK"),
        ("SUPERSEDED", "N/A"),
    ],
)
def test_result_does_not_infer_recovery_or_current_version(rig, status, cloud):
    service, source, _ = rig
    rid = prepare(service, source)
    service.store.finish(
        rid,
        status,
        {
            "tracks": {"local": "DONE", "cloud": cloud, "build": "DONE", "verify": "SKIPPED"},
            "steps": {
                "deploy.app.cloud": {
                    "status": "failed",
                    "elapsed_s": 61,
                    "error": "<script>bad()</script> adapter timeout",
                }
            },
            "infra_changes": [{"status": "applied"}],
        },
        {},
        {},
    )
    with client_for(service) as client:
        html = client.get(f"/runs/{rid}/result").text
        assert f'data-code="{cloud}"' in html
        assert 'data-code="DONE"' in html
        assert "인프라 변경 후 실행이 실패했습니다" in html
        assert "adapter timeout" in html and "&lt;script&gt;" in html
        assert "1분 1초" in html
        assert "<script>bad()" not in html
        assert "승인 기록과 배포 결과를 저장했습니다." not in html
        assert "새 버전으로 서비스 중" not in html
        if cloud != "ROLLED_BACK":
            assert "이전 버전으로 되돌렸습니다." not in html


@pytest.mark.parametrize(
    ("status", "result", "visible"),
    [
        (
            "FAILED_BEFORE_DEPLOY",
            {"phase": "settings", "detail": "<i>설정 불일치</i>"},
            "&lt;i&gt;설정 불일치&lt;/i&gt;",
        ),
        (
            "SUPERSEDED",
            {"reason": "승인 전에 새 커밋이 들어왔습니다."},
            "승인 전에 새 커밋이 들어왔습니다.",
        ),
    ],
)
def test_result_keeps_recorded_stop_reason(status, result, visible):
    run = {"run_id": "run-1", "status": status, "result": {"tracks": {}, "steps": {}, **result}}
    story = result_story(run, None, {"events": []})
    html = templates.get_template("result.html").render(
        run=run,
        result=run["result"],
        story=story,
        environment_cards=environment_cards(run, story, []),
        project="demo",
        request={"url": {"path": "/result"}},
    )
    body = html.split("</details>", 1)[0]  # 기술 정보 원문이 아니라 화면 본문에 보여야 한다.
    assert visible in body and "<i>설정 불일치</i>" not in html
    if status == "FAILED_BEFORE_DEPLOY":
        assert '<section class="notice failure" id="failure"><h2>중단 원인</h2>' in body


def test_approval_preparation_warnings_keep_code_text_and_failure_detail(rig):
    service, source, _ = rig
    rid = prepare(service, source)
    view = service.approval_view(rid)
    view["preparation_warnings"] = ["로컬 빌드용 Docker Hub 로그인 확인 필요", "hardcoded_secret"]
    view["preparation_failures"] = {"cloud": ["apply_infra"]}
    view["preparation_errors"] = {
        "cloud": {"phase": "infra", "code": "CONFIG_INVALID", "detail": "<b>요약 초과</b>"}
    }
    html = templates.get_template("approval.html").render(
        approval=view,
        project=view["project"],
        csrf_token="fixture",
        request={"url": {"path": "/approval"}},
    )
    notice = html.split("<h2>준비 경고</h2>", 1)[1].split("</section>", 1)[0]
    # 코드가 만든 경고 문장은 그대로, 사전에 있는 규칙 코드는 사람이 읽는 문장으로 바꾼다.
    assert "<li>로컬 빌드용 Docker Hub 로그인 확인 필요</li>" in notice
    assert "<li>코드에 비밀값이 직접 적혀 있습니다.</li>" in notice
    assert "클라우드 · 준비하지 못함 · 설정 입력과 연결을 확인하세요." in notice
    assert "<small>&lt;b&gt;요약 초과&lt;/b&gt;</small>" in notice and "<b>" not in notice


def test_approval_onprem_scope_comes_from_plan(rig):
    service, source, _ = rig
    rid = prepare(service, source)
    view = service.approval_view(rid)
    view["plan"]["deploy"]["cloud"]["steps"] = []
    html = templates.get_template("approval.html").render(
        approval=view,
        project=view["project"],
        csrf_token="fixture",
        request={"url": {"path": "/approval"}},
    )
    assert "승인하면 온프레미스에 배포합니다. 클라우드는 바뀌지 않습니다." in html
    assert 'data-code="N/A"' in html
    assert html.count('value="approved"') == 2
    assert html.count('value="denied"') == 2


def test_parallel_progress_events_and_confirmation():
    script = r"""
    const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
    const source=fs.readFileSync(process.argv[1],'utf8');
    let queries=0, closed=false, opened=0, connection;
    const listeners={};
    const classList=()=>({values:new Set(['hidden']),
      toggle(k,v){v?this.values.add(k):this.values.delete(k)},
      add(...keys){keys.forEach(k=>this.values.add(k))},
      remove(...keys){keys.forEach(k=>this.values.delete(k))}});
    const element=()=>({dataset:{},classList:classList(),textContent:'',items:[],
      setAttribute(){},removeAttribute(){},appendChild(item){this.items.push(item)},
      set innerHTML(v){throw Error('HTML injection')}});
    const ids=['result-link','progress-title','connection-state','current-activity',
      'activity-title','now-working','current-step-clock','total-clock','remaining-steps',
      'local-activity','cloud-activity','common-activity','event-age',
      'local-events','cloud-events','common-events','pipeline-local','pipeline-cloud',
      'pipeline-common','pipeline-final','local-progress','cloud-progress'];
    const others=Object.fromEntries(ids.map(id=>[id,element()]));
    const lane=(id,track)=>{
      const parts=Object.fromEntries(['[data-step-status]','[data-step-sentence]',
        '[data-step-age]','[data-step-detail]','[data-step-summary]'].map(key=>[key,element()]));
      return {...element(),parts,dataset:{stepRow:id,lane:track,stepState:'waiting'},
        querySelector:key=>{queries++;return parts[key]}};
    };
    const health=lane('verify.health.local','local'),
      prior=lane('deploy.was.cloud','cloud'), infra=lane('deploy.infra.cloud','cloud');
    const rows=[health,prior,infra];
    const progress={dataset:{runId:'fixture',status:'RUNNING',targets:'both',
      started:'2026-10-04T01:00:00Z',terminalStates:'["SUCCEEDED","FAILED_CLOUD"]'}};
    class EventSource {constructor(){opened++;connection=this} addEventListener(k,
      v){listeners[k]=v}close(){closed=true}}
    const document={querySelector:s=>{queries++;return s==='[data-run-id]'?progress:null},
      querySelectorAll:s=>{queries++;return s==='[data-step-row]'?rows:[]},
      getElementById:id=>{queries++;return id==='pipeline-wording'?
        {textContent:process.argv[2]}:others[id]||null},
      createElement:element};
    vm.runInNewContext(source,{document,EventSource});
    const send=(type,fields={})=>listeners[type]({data:JSON.stringify({type,...fields})});
    const before=queries;
    send('step.started',{seq:1,step:'verify.health.local',target:'local',ts:'2026-10-04T01:00:01Z'});
    assert.equal(health.dataset.stepState,'running');
    assert.ok(health.classList.values.has('is-running'));
    assert.ok(others['now-working'].classList.values.has('is-working'));
    send('step.started',{seq:2,step:'deploy.infra.cloud',target:'cloud'});
    assert.equal(prior.dataset.stepState,'unrecorded'); // no invented completion
    assert.ok(!prior.classList.values.has('complete'));
    assert.ok(health.classList.values.has('is-running')&&infra.classList.values.has('is-running'));
    assert.ok(others['current-activity'].textContent.includes('온프레미스'));
    assert.ok(others['current-activity'].textContent.includes('클라우드'));
    assert.ok(others['remaining-steps'].textContent.includes('남은 단계'));
    assert.ok(others['current-step-clock'].textContent.length>0);
    connection.onerror();
    assert.equal(progress.dataset.stream,'reconnecting');
    assert.ok(others['connection-state'].textContent.includes('다시 연결'));
    connection.onopen(); assert.equal(progress.dataset.stream,'live');
    send('step.finished',{seq:3,step:'deploy.infra.cloud',target:'cloud',status:'failed',
      detail:'<img onerror=bad()>',elapsed_s:30});
    assert.equal(infra.parts['[data-step-status]'].textContent,'실패');
    assert.ok(!infra.classList.values.has('is-running'));
    assert.ok(!others['cloud-events'].items.at(-1).textContent.includes('<img'));
    send('step.finished',{seq:3,step:'deploy.infra.cloud',status:'failed'});
    assert.equal(others['cloud-events'].items.length,2); // sequence replay deduplicated
    send('gate.opened',{seq:4,detail:'cloud_verified',target:'cloud'});
    assert.equal(infra.dataset.stepState,'failed');
    send('step.finished',{seq:5,step:'verify.health.local',target:'local',status:'succeeded',elapsed_s:3});
    assert.equal(health.dataset.stepState,'succeeded');
    assert.ok(health.classList.values.has('complete'));
    assert.ok(!health.classList.values.has('is-running'));
    assert.equal(health.parts['[data-step-detail]'].open,false);
    send('step.started',{seq:6,step:'mystery.id'}); // dynamic rows also avoid DOM lookups
    assert.equal(queries,before);
    send('run.state',{status:'FAILED_CLOUD'});
    assert.equal(progress.dataset.activity,'ended');
    assert.ok(!others['now-working'].classList.values.has('is-working'));
    assert.equal(others['activity-title'].textContent,'클라우드 배포 실패');
    assert.ok(closed&&!others['result-link'].classList.values.has('hidden'));
    assert.equal(queries,before);
    progress.dataset.status='SUCCEEDED';
    vm.runInNewContext(source,{document,EventSource}); assert.equal(opened,1);
    let submit, cancelled=false;
    const form={dataset:{},addEventListener:(name,fn)=>submit=fn};
    document.querySelector=()=>null;
    document.querySelectorAll=s=>
      s==='[data-deploy-form], [data-confirm], [data-approval-form]'?[form]:[];
    vm.runInNewContext(source,{document,window:{confirm:()=>false}});
    submit({submitter:{value:'denied'},preventDefault(){cancelled=true}});
    assert.ok(cancelled);
    cancelled=false;
    submit({submitter:{value:'approved',setAttribute(){}},preventDefault(){cancelled=true}});
    assert.equal(cancelled,false); // primary approval stays one click
    const field=value=>({value,events:{},addEventListener(k,fn){this.events[k]=fn;}});
    const selection=field('both'), dns=field('route53'), domain=field('app.example.com');
    const zone=field('');
    const cloudDetails={open:false,querySelector:()=>domain};
    const zoneRow={hidden:false,querySelector:()=>zone};
    const settings={
      "select[name='default_targets']":selection, "[data-cloud-settings]":cloudDetails,
      "select[name='dns_mode']":dns, "[data-zone-field]":zoneRow,
    };
    document.querySelector=s=>settings[s]||null;
    document.querySelectorAll=()=>[];
    vm.runInNewContext(source,{document});
    assert.ok(domain.required&&zone.required&&cloudDetails.open);
    selection.value='onprem';selection.events.change();
    assert.equal(domain.required,false);
    assert.ok(zone.required&&!zoneRow.hidden&&cloudDetails.open);
    domain.value='';domain.events.input();
    assert.equal(zone.required,false);
    dns.value='external';dns.events.change();
    assert.ok(zoneRow.hidden);
    selection.value='cloud';selection.events.change();
    assert.ok(domain.required&&cloudDetails.open);
    """
    package = distribution("nodejs-wheel-binaries")
    node = next(package.locate_file(f) for f in package.files or () if str(f).endswith("/bin/node"))
    root = Path(__file__).resolve().parents[3]
    subprocess.run(
        [
            str(node),
            "-e",
            script,
            str(root / "src/ddak/web/static/app.js"),
            json.dumps({"texts": TEXT, "aliases": ALIASES}),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )


@pytest.mark.parametrize("added", [None, 42, {}, "record", [None, {"actions": None}]])
def test_approval_handles_unstructured_iam_metadata(rig, added):
    from tests.unit.test_approval_meta import HASH, summary

    service, source, _ = rig
    infra = summary()
    infra["iam_diff"][0]["added"] = added
    rid = prepare(service, source, subjects={"infra": HASH}, infra_summary=infra)
    with client_for(service) as client:
        html = client.get(f"/runs/{rid}/approval")
        assert html.status_code == 200
        assert "aws_iam_role_policy.exec_secrets" in html.text
        assert HASH in html.text


def test_approval_shows_producer_proposed_permissions_before_technical_details(rig):
    from tests.unit.test_approval_meta import HASH, summary

    service, source, _ = rig
    infra = summary()
    change = infra["iam_diff"][0]
    change["proposed_allow"] = [{"actions": ["s3:GetObject"], "resources": ["fixture-resource"]}]
    change.pop("added")
    rid = prepare(service, source, subjects={"infra": HASH}, infra_summary=infra)
    with client_for(service) as client:
        html = client.get(f"/runs/{rid}/approval").text
        visible = html.split("<details><summary>기술 정보</summary>")[0]
        assert "제안 정책의 전체 허용 목록" in visible
        assert "s3:GetObject" in visible and "fixture-resource" in visible


class ReviewControls(HTMLParser):
    """브라우저가 전송할 수 있는 input과 표시된 동작을 검사한다."""

    def __init__(self, html):
        super().__init__()
        self.controls = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag in {"input", "textarea", "button"}:
            self.controls.append((tag, dict(attrs)))

    def named(self, name):
        return [attrs for _, attrs in self.controls if attrs.get("name") == name]

    def inputs(self):
        return {
            attrs["name"]: attrs.get("value", "on" if attrs.get("type") == "checkbox" else "")
            for tag, attrs in self.controls
            if tag == "input"
            and attrs.get("name")
            and "disabled" not in attrs
            and (attrs.get("type") != "checkbox" or "checked" in attrs)
        }


@pytest.fixture
def review_ui(rig):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    service, source, calls = rig
    rid = prepare(service, source)
    proposals = [
        {
            "id": name,
            "title": title,
            "reason": "설정 검토",
            "revision": 1,
            "requires": [],
            "env_vars": ["APP_CONFIG"],
            "diff": "-before\n+<script>fixture()</script>",
        }
        for name, title in (("retained", "이전 설정"), ("config-file", "파일 설정 묶음"))
    ]
    proposals[0]["required"] = True
    review = {
        "state": "ready",
        "revision": 3,
        "busy": False,
        "proposals": proposals,
        "items": proposals,
        "selected": [],
        "notes": {"config-file": "기존 요청"},
        "candidate": None,
        "source": "fixture",
    }
    manager = SimpleNamespace(
        get=Mock(side_effect=lambda _: review),
        view=Mock(side_effect=lambda _: review),
        begin=Mock(),
        request=Mock(),
        shutdown=AsyncMock(),
    )
    service.patch_reviews = manager
    return service, rid, calls, manager, review


def test_required_review_stays_checked_posts_hidden_and_has_no_prompt(review_ui):
    service, rid, calls, manager, _ = review_ui
    with client_for(service) as client:
        response = client.get(f"/runs/{rid}/patch-review")
        assert response.status_code == 200
        html = response.text
        controls = ReviewControls(html)
        checkbox, hidden = controls.named("apply_retained")
        assert checkbox["type"] == "checkbox" and {"checked", "disabled"} <= checkbox.keys()
        assert checkbox["aria-describedby"] == "required-retained"
        assert hidden["type"] == "hidden" and hidden["value"] == "on"
        assert not controls.named("prompt_retained")
        assert "revise:retained" not in html and "이전 승인 수정 유지" in html
        assert "2개 중 1개 선택" in html
        assert controls.named("prompt_config-file")[0]["maxlength"] == "2000"
        assert "기존 요청" in html and "같은 파일의 설정 수정은 하나로 묶어" in html
        assert "<script>fixture()" not in html and "&lt;script&gt;fixture()" not in html
        assert "새 제안을 모두 제외해도 이전 승인 수정은 유지" in html
        response = post(
            client,
            f"/runs/{rid}/patch-review",
            **{
                **controls.inputs(),
                "action": "revise:config-file",
                "prompt_config-file": "수정 요청",
            },
        )
        assert response.status_code == 303
        manager.request.assert_called_once_with(
            rid,
            3,
            "revise:config-file",
            ["retained"],
            prompt="수정 요청",
            candidate_id="",
            notes={"config-file": "수정 요청"},
        )
        assert not calls.contexts and not service.get_approvals(rid)


def test_review_error_keeps_required_selection_and_user_prompt(review_ui):
    from ddak.core.contracts.errors import DdakToolError, ErrorCode

    service, rid, _, manager, _ = review_ui
    manager.request.side_effect = DdakToolError(ErrorCode.PRECONDITION_FAILED, "다시 확인")
    with client_for(service) as client:
        client.get(f"/runs/{rid}/patch-review")
        response = post(
            client,
            f"/runs/{rid}/patch-review",
            action="revise:config-file",
            revision="3",
            _form_id="patch-review",
            _return_to=f"/runs/{rid}/patch-review",
            **{"apply_config-file": "on", "prompt_config-file": "수정 요청 <확인>"},
        )
        assert response.status_code == 303
        response = client.get(response.headers["location"])
        assert "PRECONDITION_FAILED" in response.text
        assert "다시 확인" in response.text and "수정 요청 &lt;확인&gt;" in response.text
        assert "2개 중 2개 선택" in response.text
        assert "checked" in ReviewControls(response.text).named("apply_retained")[0]


@pytest.mark.parametrize("action", ["adopt", "keep"])
def test_candidate_requires_explicit_choice_before_finalizing(review_ui, action):
    service, rid, calls, manager, review = review_ui
    review["candidate"] = {
        "id": "candidate-2",
        "proposal": review["proposals"][1],
        "diff": "+new candidate",
    }
    with client_for(service) as client:
        html = client.get(f"/runs/{rid}/patch-review").text
        controls = ReviewControls(html)
        buttons = {a["value"]: a for a in controls.named("action")}
        assert "disabled" in buttons["finalize"]
        assert "disabled" in buttons["revise:config-file"]
        assert "disabled" not in buttons["adopt"] and "disabled" not in buttons["keep"]
        assert "현재 제안은 아직 바뀌지 않았습니다" in html
        assert not manager.request.called
        response = post(client, f"/runs/{rid}/patch-review", **controls.inputs(), action=action)
        assert response.status_code == 303
        assert manager.request.call_args.args == (rid, 3, action, ["retained"])
        assert manager.request.call_args.kwargs["candidate_id"] == "candidate-2"
        assert not calls.contexts and not service.get_approvals(rid)


@pytest.mark.parametrize(
    "status,code",
    [
        ("generating", "PATCH_GENERATING"),
        ("reviewing", "PATCH_REVIEWING"),
        ("finalizing", "PATCH_FINALIZING"),
        ("publishing", "PATCH_PUBLISHING"),
    ],
)
def test_review_busy_uses_shared_state_and_waits_for_new_approval(review_ui, status, code):
    service, rid, calls, _, review = review_ui
    review.update(state=status, busy=True)
    with client_for(service) as client:
        html = client.get(f"/runs/{rid}/patch-review").text
        assert f'class="state running" data-code="{code}"' in html
        assert 'aria-busy="true"' in html and "data-live-region" in html
        assert 'name="action"' not in html
        if status == "finalizing":
            assert "배포 계획을 다시 검사" in html and "새 승인 화면" in html
        if status == "publishing":
            assert "새 승인 자료 연결 중" in html
        approval = client.get(f"/runs/{rid}/approval").text
        assert "disabled" in next(
            b for b in ReviewControls(approval).named("decision") if b["value"] == "approved"
        )
        assert not calls.contexts and not service.get_approvals(rid)


def test_publishing_poll_moves_to_sealed_approval_but_allows_new_review(review_ui):
    service, rid, _, _, review = review_ui
    review.update(state="publishing", busy=True)
    with client_for(service) as client:
        html = client.get(f"/runs/{rid}/patch-review").text
        polling = f"/runs/{rid}/patch-review?wait_for_approval=1"
        assert f'data-live-url="{polling}"' in html
        assert client.get(polling, follow_redirects=False).status_code == 200
        review.update(state="sealed", busy=False)
        response = client.get(polling, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"/runs/{rid}/approval"
        html = client.get(f"/runs/{rid}/patch-review").text
        assert 'value="begin"' in html


def test_review_close_and_successor_return_to_approval_without_deployment(review_ui):
    service, rid, calls, manager, review = review_ui
    with client_for(service) as client:
        html = client.get(f"/runs/{rid}/patch-review").text
        assert "이전 승인 화면으로 돌아가며 배포하지 않습니다" in html
        response = post(client, f"/runs/{rid}/patch-review", revision="3", action="cancel")
        assert response.status_code == 303
        assert response.headers["location"] == f"/runs/{rid}/approval"
        assert manager.request.call_args.args[2] == "cancel"
        review["successor"] = "review-successor"
        response = client.get(f"/runs/{rid}/patch-review", follow_redirects=False)
        assert response.headers["location"] == "/runs/review-successor/approval"
        assert not calls.contexts and not service.get_approvals(rid)


def test_approval_keeps_main_context_and_single_final_decision(review_ui, monkeypatch):
    service, rid, _, _, review = review_ui
    raw_patch = "+fixture-private-patch-content"
    view = {
        **service.approval_view(rid),
        "patch": raw_patch,
        "patch_warning": "이전 승인 패치 확인 <warning>",
        "missing_env_keys": ["APP_CONFIG"],
        "patch_meta": {
            "reason": "환경 패치 확인",
            "passed": True,
            "gitleaks": "passed",
            "new_env_keys": ["APP_CONFIG"],
        },
        "decision_basis": {"provider": "fixture-provider", "reason": "fixture-reason"},
    }
    original_subjects = dict(view["subjects"])
    monkeypatch.setattr(service, "approval_view", lambda _: view)
    with client_for(service) as client:
        html = client.get(f"/runs/{rid}/approval").text
        assert 'href="/setup?project=flaskr-three"' in html
        for text in (
            "이전 승인 수정 확인이 필요합니다.",
            "값 필요",
            "APP_CONFIG",
            "수정 코드 검사 통과",
            "fixture-provider",
            "fixture-reason",
        ):
            assert text in html
        buttons = ReviewControls(html).named("decision")
        assert len(buttons) == 4 and "disabled" in next(
            b for b in buttons if b["value"] == "approved"
        )
        review.update(state="sealed", selected=["retained"])
        html = client.get(f"/runs/{rid}/approval").text
        assert html.count('value="approved"') == 2
        assert "disabled" not in next(
            b for b in ReviewControls(html).named("decision") if b["value"] == "approved"
        )
        assert "이전 승인 수정 유지" in html
        assert raw_patch not in html
        assert view["patch"] == raw_patch and view["subjects"] == original_subjects
