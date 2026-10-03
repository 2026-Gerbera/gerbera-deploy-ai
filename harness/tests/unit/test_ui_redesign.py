"""source=fixture: 원장 UI의 환경 분리와 기존 POST 경계 회귀."""

import subprocess
from html.parser import HTMLParser
from importlib.metadata import distribution
from pathlib import Path

import pytest

from ddak.web.dependencies import templates
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
        assert "<script>bad()" not in html
        assert "승인 기록과 배포 결과를 저장했습니다." not in html
        assert "새 버전으로 서비스 중" not in html
        if cloud != "ROLLED_BACK":
            assert "이전 버전으로 되돌렸습니다." not in html


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
    assert "승인하면 온프렘에 배포합니다. 클라우드는 바뀌지 않습니다." in html
    assert 'data-code="N/A"' in html
    assert html.count('value="approved"') == 1
    assert html.count('value="denied"') == 1


def test_parallel_progress_events_and_confirmation():
    script = r"""
    const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
    const source=fs.readFileSync(process.argv[1],'utf8');
    let queries=0, closed=false, opened=0;
    const listeners={};
    const classList=()=>({values:new Set(['hidden']),
       toggle(k,v){v?this.values.add(k):this.values.delete(k)},
      remove(k){this.values.delete(k)}});
    const phases=['plan','infra','build','deploy','verify'];
    const nodes=['local','cloud'].flatMap(track=>phases.map(phase=>({dataset:{track,
      phase},classList:classList(),textContent:'확인 중'})));
    const lists=Object.fromEntries(['local-events',
      'cloud-events','common-events'].map(id=>[id,
      {items:[],querySelector:()=>null,appendChild(item){this.items.push(item)}}]));
    const others=Object.fromEntries(['result-link',
      'progress-title','connection-state','current-activity'].map(id=>[id,
      {classList:classList(),textContent:''}]));
    const progress={dataset:{runId:'fixture',status:'RUNNING',targets:'both',
      terminalStates:'["SUCCEEDED","FAILED_CLOUD"]'}};
    class EventSource {constructor(){opened++} addEventListener(k,
      v){listeners[k]=v}close(){closed=true}}
    const document={querySelector:s=>s==='[data-run-id]'?progress:null,
      querySelectorAll:s=>{if(s==='[data-phase]'){queries++;return nodes}return []},
      getElementById:id=>lists[id]||others[id]||null,
      createElement:()=>({set innerHTML(v){throw Error('HTML injection')}})};
    vm.runInNewContext(source,{document,EventSource});
    const send=(type,fields={})=>listeners[type]({data:JSON.stringify({type,...fields})});
    const cell=(track,phase)=>nodes.find(n=>n.dataset.track===track&&n.dataset.phase===phase);
    send('step.started',{seq:1,step:'verify.health.local',target:'local'});
    assert.equal(cell('local','verify').textContent,'진행 중');
    assert.equal(cell('local','plan').textContent,'확인 중'); // no invented completion
    send('step.started',{seq:2,step:'deploy.infra.cloud',target:'cloud'});
    assert.equal(cell('local','verify').textContent,'진행 중');
    assert.equal(cell('cloud','infra').textContent,'진행 중');
    send('step.started',{seq:3,step:'deploy.was.local',target:'local'});
    assert.ok(cell('local','verify').classList.values.has('active')); // no backwards jump
    send('step.finished',{seq:4,step:'deploy.infra.cloud',
      target:'cloud',status:'failed',detail:'<img onerror=bad()>',
      elapsed_s:30});
    assert.equal(cell('cloud','infra').textContent,'실패');
    assert.ok(lists['cloud-events'].items.at(-1).textContent.includes('<img onerror=bad()>'));
    assert.ok(lists['cloud-events'].items.at(-1).className.includes('failed'));
    const before=queries;
    send('step.finished',{seq:4,step:'deploy.infra.cloud',target:'cloud',status:'failed'});
    assert.equal(queries,before); // replay deduplicated
    send('gate.opened'); assert.equal(queries,before);
    send('step.started',{seq:5,step:'mystery.id'}); assert.equal(queries,before+1);
    assert.equal(cell('local','plan').textContent,'확인 중');
    send('run.state',{status:'FAILED_CLOUD'});
    assert.ok(closed); assert.ok(!others['result-link'].classList.values.has('hidden'));
    assert.equal(others['progress-title'].textContent,'클라우드 배포 실패');
    progress.dataset.status='SUCCEEDED';
    vm.runInNewContext(source,{document,EventSource}); assert.equal(opened,1);
    // Non-target cloud cells stay untouched for a shared build event.
    progress.dataset.status='RUNNING';
    nodes.filter(n=>n.dataset.track==='cloud').forEach(n=>{
      n.dataset.excluded='true';n.textContent='대상 아님';
    });
    vm.runInNewContext(source,{document,EventSource});
    send('step.started',{step:'build.was'});
    assert.equal(cell('cloud','build').textContent,'대상 아님');
    progress.dataset.targets='unknown';
    nodes.forEach(n=>{n.dataset.excluded='false';n.textContent='확인 중'});
    vm.runInNewContext(source,{document,EventSource});
    send('step.finished',{step:'build.was',status:'succeeded'});
    assert.equal(cell('cloud','build').textContent,'확인 중');
    assert.equal(cell('local','build').textContent,'확인 중');
    send('step.started',{step:'deploy.was.local',target:'local'});
    assert.equal(cell('local','deploy').textContent,'진행 중');
    assert.equal(cell('cloud','deploy').textContent,'확인 중');
    let submit, cancelled=false;
    const form={dataset:{},addEventListener:(name,fn)=>submit=fn};
    document.querySelector=()=>null;
    document.querySelectorAll=s=>s==='[data-confirm], [data-approval-form]'?[form]:[];
    vm.runInNewContext(source,{document,window:{confirm:()=>false}});
    submit({submitter:{value:'denied'},preventDefault(){cancelled=true}});
    assert.ok(cancelled);
    cancelled=false; submit({submitter:{value:'approved'},preventDefault(){cancelled=true}});
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
        [str(node), "-e", script, str(root / "src/ddak/web/static/app.js")],
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
