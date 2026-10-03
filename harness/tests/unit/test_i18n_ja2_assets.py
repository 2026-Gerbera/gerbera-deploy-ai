"""PR2: 외부 연결 없는 Node DOM으로 코드 질문 표시·원문 보존을 검증한다."""

import json
import re
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[3] / "src/ddak/web"
WORDING = runpy.run_path(str(WEB / "js_wording.py"))["wording"]

SCRIPT = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2], dictionary = process.argv[3];
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.own = ''; this.children = [];
    this.attrs = new Map(); this.handlers = {}; this.hidden = false; this.dataset = {}; }
  set textContent(value) { this.own = String(value); this.children = []; }
  get textContent() {
    return this.own + this.children.map(c => typeof c === 'string' ? c : c.textContent).join('');
  }
  set innerHTML(_) { throw Error('HTML 해석 금지'); }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.own = ''; this.children = []; }
  setAttribute(key, value) { this.attrs.set(key, value); }
  removeAttribute(key) { this.attrs.delete(key); }
  addEventListener(name, fn) { this.handlers[name] = fn; }
  focus() { this.focused = true; }
}
const parts = Object.fromEntries(
  ['toggle','panel','form','input','submit','answer'].map(k => [k,new Element()]));
parts.panel.hidden = true;
parts.input.value = scenario === 'empty' ? '  ' : '한국어 사용자 질문 <b>그대로</b>';
const root = new Element();
root.dataset = {url:'/code-qa/ask',project:'한국어-project',csrf:'fixture'};
root.querySelector = s => parts[s.slice('[data-code-qa-'.length, -1)];
const document = {getElementById:id => id === 'app-wording' ? {textContent:dictionary} : null,
  querySelectorAll: s => s === '[data-code-qa]' ? [root] : [],
  createElement:tag => new Element(tag)};
let now = 0, timer, calls = 0, cleared = 0, request, resolve;
const pending = new Promise(r => { resolve = r; });
const window = {URLSearchParams,
  setInterval:fn => { timer = fn; return 7; },
  clearInterval:id => { assert.equal(id,7); cleared++; },
  fetch:async (url,options) => { calls++;
    request = {url,body:options.body.toString(),method:options.method};
    if (scenario === 'network') throw Error('internal-private-exception');
    return pending; },
};
if (scenario === 'unsupported') delete window.fetch;
class Clock extends Date { static now() { return now; } }
async function run() {
  if (scenario === 'no-window') {
    vm.runInNewContext(source,{document}); console.log('{}'); return;
  }
  if (scenario === 'no-document') {
    vm.runInNewContext(source,{window}); console.log('{}'); return;
  }
  vm.runInNewContext(source,{document,window,Date:Clock});
  parts.toggle.handlers.click();
  assert.equal(parts.panel.hidden,false); assert.ok(parts.input.focused);
  let prevented = 0;
  const event = {preventDefault() { prevented++; }};
  const task = parts.form.handlers.submit(event);
  const initial = parts.answer.textContent;
  let running = initial;
  if (calls && scenario !== 'network') {
    assert.equal(parts.submit.disabled,true);
    assert.equal(parts.submit.attrs.get('aria-busy'),'true');
    await parts.form.handlers.submit(event); assert.equal(calls,1);
    now = 3000; timer(); running = parts.answer.textContent;
    if (scenario === 'server-error') resolve({ok:false,json:async()=>({ok:false,
      error:{code:'TOOL_FAILURE',message:'한국어 기술 오류 <b>원문</b>'}})});
    else if (scenario === 'malformed') resolve({ok:false,json:async()=>{throw Error('raw html');}});
    else resolve({ok:true,json:async()=>({ok:true,commit:'a1b2c3d',
      branch:'한국어-{seconds}',files:2,
      truncated:true,answer:'한국어 AI 답변 <img src=x onerror=fail()>',
      sources:['한국어/소스.py']})});
  }
  await task;
  if (calls) {
    assert.equal(parts.submit.disabled,false);
    assert.equal(parts.submit.attrs.has('aria-busy'),false); assert.equal(cleared,1);
  }
  assert.ok(prevented);
  console.log(JSON.stringify({initial,running,final:parts.answer.textContent,calls,request,
    role:parts.answer.attrs.get('role'),tags:parts.answer.children.map(c=>c.tagName)}));
}
run().catch(error => {console.error(error);process.exitCode=1;});
"""


def run_qa(scenario, dictionary):
    result = subprocess.run(
        [
            shutil.which("node") or "node",
            "-e",
            SCRIPT,
            str(WEB / "static/code_qa.js"),
            scenario,
            dictionary,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("language", ["ko", "ja"])
@pytest.mark.parametrize(
    "scenario", ["success", "empty", "unsupported", "network", "server-error", "malformed"]
)
def test_qa_translates_only_ui_and_preserves_request_response(language, scenario):
    words = WORDING(language)
    result = run_qa(scenario, json.dumps(words))
    if result["calls"]:
        from urllib.parse import parse_qs

        assert result["request"]["url"] == "/code-qa/ask"
        assert result["request"]["method"] == "POST"
        assert parse_qs(result["request"]["body"]) == {
            "csrf_token": ["fixture"],
            "project": ["한국어-project"],
            "question": ["한국어 사용자 질문 <b>그대로</b>"],
        }
        assert result["initial"] == words["qa.loading"].replace("{seconds}", "0")
        if scenario != "network":
            assert result["running"] == words["qa.loading"].replace("{seconds}", "3")
    if scenario == "success":
        assert result["tags"] == ["p", "pre", "p"]
        assert "한국어 AI 답변 <img src=x onerror=fail()>" in result["final"]
        assert "한국어/소스.py" in result["final"]
        assert (
            "한국어-{seconds}" in result["final"]
        )  # 데이터 안의 자리표시자도 다시 보간하지 않는다.
        assert words["qa.commit"] + "a1b2c3d" in result["final"]
        assert words["qa.sources"] in result["final"]
        assert words["qa.truncated"] in result["final"]
    else:
        assert result["role"] == "alert"
        if scenario == "server-error":
            assert result["final"] == "TOOL_FAILURE한국어 기술 오류 <b>원문</b>"
        else:
            key = {
                "empty": "required",
                "unsupported": "unsupported",
                "network": "network",
                "malformed": "failed",
            }[scenario]
            code = "CONFIG_INVALID" if scenario == "empty" else "REQUEST_FAILED"
            assert result["final"] == code + words["qa." + key]


@pytest.mark.parametrize("dictionary", ["{", "null", "[]", '{"qa.required":42}', "{}"])
def test_qa_missing_or_invalid_dictionary_uses_ko(dictionary):
    assert run_qa("empty", dictionary)["final"] == "CONFIG_INVALID질문을 입력하세요."


@pytest.mark.parametrize("scenario", ["no-window", "no-document"])
def test_qa_without_browser_globals_is_safe(scenario):
    assert run_qa(scenario, "{}") == {}


def test_qa_fallback_matches_server_dictionary():
    source = (WEB / "static/code_qa.js").read_text()
    fallback = json.loads(source.split("const fallbackWording = ", 1)[1].split(";", 1)[0])
    assert fallback == {key: value for key, value in WORDING("ko").items() if key.startswith("qa.")}
    assert all(
        not re.search(r"[가-힣]", value)
        for key, value in WORDING("ja").items()
        if key.startswith("qa.")
    )


def test_layout_adjustments_are_ja_scoped_and_keep_sidebar_width():
    css = (WEB / "static/app.css").read_text()
    assert "--sidebar-width: 224px" in css
    adjustments = css.split("/* JA는 224px 탐색 폭을 유지", 1)[1].split("*/", 1)[1]
    for selectors in re.findall(r"([^{}]+)\{", adjustments):
        if selectors.strip().startswith("@media"):
            continue
        assert all(":lang(ja)" in selector for selector in selectors.split(","))
    # 일본어 상태/버튼을 숨기거나 잘라내는 방식으로 overflow를 해결하지 않는다.
    assert "overflow: hidden" not in adjustments and "text-overflow: ellipsis" not in adjustments
    assert "white-space: normal" in adjustments and "min-width: 0" in adjustments


DEMO_SCRIPT = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const state = process.argv[2], dictionary = process.argv[3];
const raw = '<p>한국어 서버 원문 · TOOL_FAILURE · v3</p>';
let calls = 0, delays = [], html = null;
const button = {disabled:true,textContent:'initial'};
const tag = {dataset:{demoV3:state}};
const box = {dataset:{url:'/ops/demo/status?project=fixture'},textContent:'',
  set innerHTML(value) { html = value; },
  querySelector: selector => { assert.equal(selector,'[data-demo-v3]'); return tag; },
};
const document = {getElementById:id => ({'demo-state':box,'demo-prepare-v3':button,
  'app-wording':{textContent:dictionary}})[id]};
const fetch = async (url, options) => {
  calls++; assert.equal(url,box.dataset.url); assert.equal(options.credentials,'same-origin');
  if (state === 'network') throw Error('한국어 internal 원문');
  return {ok:state !== 'http-error',text:async()=>raw};
};
async function run() {
  let finish;
  const finished = new Promise(resolve => { finish = resolve; });
  vm.runInNewContext(source,{document,fetch,
    setTimeout:(_, delay)=>{ delays.push(delay); finish(); }});
  await finished;
  assert.equal(calls,1); assert.deepEqual(delays,[15000]);
  console.log(JSON.stringify({label:button.textContent,disabled:button.disabled,
    message:box.textContent,html,state:tag.dataset.demoV3}));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
"""


@pytest.mark.parametrize("language", ["ko", "ja"])
@pytest.mark.parametrize("state", ["ready", "missing", "failed", "network", "http-error"])
def test_demo_button_uses_dictionary_preserves_status_html(language, state):
    words = WORDING(language)
    result = subprocess.run(
        [
            shutil.which("node") or "node",
            "-e",
            DEMO_SCRIPT,
            str(WEB / "static/demo_reset.js"),
            state,
            json.dumps(words),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert value["state"] == state
    if state in ("network", "http-error"):
        assert value["message"] == words["demo.status_failed"]
        assert value["html"] is None
    else:
        assert value["label"] == words["demo." + state]
        assert value["disabled"] is (state != "ready")
        assert value["html"] == "<p>한국어 서버 원문 · TOOL_FAILURE · v3</p>"


def test_demo_fallback_matches_server_dictionary():
    source = (WEB / "static/demo_reset.js").read_text()
    fallback = json.loads(source.split("const fallbackWording = ", 1)[1].split(";", 1)[0])
    assert fallback == {
        key: value for key, value in WORDING("ko").items() if key.startswith("demo.")
    }
