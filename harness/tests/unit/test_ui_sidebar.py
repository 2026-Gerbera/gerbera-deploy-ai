"""source=fixture: 프로젝트 탐색과 접힘 상태가 배포 동작을 바꾸지 않는다."""

import subprocess
from importlib.metadata import distribution
from pathlib import Path

import pytest

from tests.unit import test_deployment_service as support
from tests.unit.test_ui_integration_fix10 import client_for, prepare

rig = support.rig


@pytest.mark.parametrize("page", ["home", "approval", "progress", "result", "settings", "ops"])
def test_sidebar_projects_and_selected_context_across_pages(rig, page):
    service, source, calls = rig
    rid = prepare(service, source)
    service.save_project_settings("other-project", {}, updated_by="fixture", expected_version=0)
    if page == "progress":
        service.approve(rid, approver="fixture")
    if page == "result":
        service.store.finish(rid, "SUCCEEDED", {"tracks": {"local": "DONE"}}, {}, {})
    paths = {
        "home": "/projects",
        "approval": f"/runs/{rid}/approval",
        "progress": f"/runs/{rid}/progress",
        "result": f"/runs/{rid}/result",
        "settings": "/settings?project=flaskr-three",
        "ops": "/ops?project=flaskr-three",
    }
    with client_for(service) as client:
        html = client.get(paths[page]).text
        sidebar = html.split('<aside id="sidebar"', 1)[1].split("</aside>", 1)[0]
        assert 'href="/projects"' in sidebar
        assert 'href="/?project=other-project"' in sidebar
        assert 'href="/?project=flaskr-three"' in sidebar
        assert "<form" not in sidebar
        if page == "home":
            assert "<h1>전체 프로젝트</h1>" in html
            assert 'href="/projects" aria-current="page"' in sidebar
            assert 'aria-label="현재 프로젝트 화면"' not in sidebar
        else:
            assert 'href="/ops?project=flaskr-three"' in sidebar
            assert 'href="/settings?project=flaskr-three"' in sidebar
            assert 'href="/?project=flaskr-three" aria-current="true"' in sidebar
        switched = client.get("/?project=other-project").text
        assert "<h1>other-project</h1>" in switched
        assert 'href="/settings?project=other-project"' in switched
        assert not calls.contexts


def test_sidebar_works_with_no_projects_or_an_unsaved_project(rig):
    service, _, _ = rig
    with client_for(service) as client:
        html = client.get("/projects").text
        assert "등록된 프로젝트가 없습니다" in html
        assert '<aside id="sidebar" class="sidebar"' in html  # JS 없이도 탐색 노출
        html = client.get("/settings?project=unsaved-project").text
        assert 'href="/?project=unsaved-project" aria-current="true"' in html
        assert service.list_projects() == []


def test_sidebar_toggle_storage_failure_escape_and_reload():
    script = r"""
    const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
    const code=fs.readFileSync(process.argv[1],'utf8');
    function render(saved, narrow=false, blocked=false){
      const state={saved}, handlers={}, shell={dataset:{}}, events={};
      const sidebar={hidden:false,contains:node=>node==='link',
        addEventListener:(name,fn)=>events[name]=fn};
      const toggle={hidden:true,attrs:{},textContent:'',
        setAttribute(k,v){this.attrs[k]=v},focus(){document.activeElement=this},
        addEventListener:(name,fn)=>handlers[name]=fn};
      const document={activeElement:null,querySelectorAll:()=>[],
        querySelector:s=>({'#sidebar':sidebar,'[data-sidebar-toggle]':toggle,
          '.app-shell':shell}[s]||null)};
      const window={matchMedia:()=>({matches:narrow}),addEventListener(){}};
      Object.defineProperty(window,'localStorage',{get(){
        if(blocked)throw Error('storage denied');
        return {getItem:()=>state.saved,setItem:(_,v)=>state.saved=v};
      }});
      vm.runInNewContext(code,{document,window});
      return {document,sidebar,toggle,shell,events,handlers,state};
    }
    const ui=render(null);
    assert.equal(ui.sidebar.hidden,false);assert.equal(ui.toggle.hidden,false);
    ui.handlers.click();
    assert.equal(ui.sidebar.hidden,true);
    assert.equal(ui.toggle.attrs['aria-expanded'],'false');
    assert.equal(ui.shell.dataset.sidebarOpen,'false');
    assert.equal(ui.state.saved,'closed');
    const reloaded=render(ui.state.saved);
    assert.equal(reloaded.sidebar.hidden,true);
    reloaded.handlers.click();reloaded.document.activeElement='link';
    let prevented=false;
    reloaded.events.keydown({key:'Escape',preventDefault(){prevented=true}});
    assert.ok(prevented);assert.equal(reloaded.sidebar.hidden,true);
    assert.equal(reloaded.document.activeElement,reloaded.toggle);
    const unavailable=render(null,false,true);
    unavailable.handlers.click();assert.equal(unavailable.sidebar.hidden,true);
    unavailable.handlers.click();assert.equal(unavailable.sidebar.hidden,false);
    assert.equal(render(null,true).sidebar.hidden,true);
    assert.equal(render('open',true).sidebar.hidden,false);
    const noWindow={querySelector:()=>null,querySelectorAll:()=>[]};
    vm.runInNewContext(code,{document:noWindow});
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
