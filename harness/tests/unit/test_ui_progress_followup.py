"""source=fixture: 실제 통신 없이 진행 화면 렌더와 SSE 갱신을 검증한다."""

import json
import re
import subprocess
from importlib.metadata import distribution
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ddak.web.narrative import pipeline_view, wording

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "src/ddak/web"

SCRIPT = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8'), scenario = process.argv[2];
let queries = 0, phaseQueries = 0;
class Node {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.dataset = {}; this.children = [];
    this.attrs = new Map(); this.classes = new Set(); this.hidden = false; this.open = false;
    this.ownText = '';
    this.classList = {
      add: (...names) => names.forEach(name => this.classes.add(name)),
      remove: (...names) => names.forEach(name => this.classes.delete(name)),
      toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name),
      contains: name => this.classes.has(name),
    };
  }
  set className(value) { this.classes = new Set(value.split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(' '); }
  // Real DOM textContent destroys children; a plain property would miss the icon regression.
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() {
    return this.ownText + this.children.map(child => child.textContent).join('');
  }
  set innerHTML(_) { throw Error('Unexpected HTML insertion'); }
  appendChild(node) { this.children.push(node); return node; }
  setAttribute(key, value) { this.attrs.set(key, value); }
  removeAttribute(key) { this.attrs.delete(key); }
  getAttribute(key) { return this.attrs.get(key) ?? null; }
  matches(selector) {
    if (selector.startsWith('.')) return this.classes.has(selector.slice(1));
    const data = /^\[data-([a-z-]+)\]$/.exec(selector);
    if (data) return data[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase()) in this.dataset;
    return this.tagName === selector.toUpperCase();
  }
  querySelector(selector) {
    queries++;
    for (const node of this.children) {
      if (node.matches(selector)) return node;
      const found = node.querySelector(selector); if (found) return found;
    }
    return null;
  }
}
const part = (tag, key, text = '') => {
  const node = new Node(tag); node.dataset[key] = ''; node.textContent = text; return node;
};
function row(id, track, state = 'waiting', started = '') {
  const node = new Node('li'); node.dataset = {stepRow: id, lane: track, stepState: state, started};
  node.className = 'pipeline-step';
  const detail = part('details', 'stepDetail'); detail.open = state !== 'succeeded';
  const summary = part('summary', 'stepSummary'), title = part('strong', 'stepTitle', '초기 작업');
  const outcome = part('span', 'stepOutcome'), wrapper = part('span', 'stepStatus');
  const badge = new Node('span'), icon = new Node('i'), label = new Node('span');
  badge.className = 'state waiting'; icon.setAttribute('aria-hidden', 'true');
  label.textContent = '대기';
  badge.appendChild(icon); badge.appendChild(label); wrapper.appendChild(badge);
  summary.appendChild(title); summary.appendChild(outcome); summary.appendChild(wrapper);
  const sentence = part('p', 'stepSentence', '서버 기록'), age = part('span', 'stepAge');
  detail.appendChild(summary); detail.appendChild(sentence); detail.appendChild(age);
  node.appendChild(detail);
  return {node, detail, summary, title, outcome, wrapper, badge, icon, label, sentence, age};
}
const first = '2026-10-03T15:00:00Z';
const local = row('verify.health.local', 'local', 'running', '2026-10-03T15:01:30Z');
const infra = row('deploy.infra.cloud', 'cloud'), earlier = row('deploy.was.cloud', 'cloud');
if (scenario === 'replay') {
  infra.node.dataset.stepState = 'succeeded'; infra.node.dataset.started = first;
  infra.sentence.textContent = 'S3 버킷 삭제를 마쳤습니다. (변경 3)';
}
const rows = [local, earlier, infra];
const ids = ['progress-title','activity-title','current-activity',
  'candidate-activity','now-working',
  'current-step-clock','total-clock','remaining-steps','result-link','event-age','connection-state','pipeline-final'];
for (const track of ['local','cloud','common']) ids.push(`${track}-activity`,`${track}-work-note`,
  `${track}-work-age`,`${track}-events`,`pipeline-${track}`,`${track}-progress`,`${track}-progress-label`);
const nodes = Object.fromEntries(ids.map(id => [id, new Node()]));
nodes['local-work-age'].dataset.laneStarted = first;
nodes['cloud-work-age'].dataset.laneStarted = '';
const progress = new Node();
progress.dataset = {runId: 'fixture', status: 'RUNNING', started: first,
  terminalStates: '["SUCCEEDED","FAILED_CLOUD"]', stream: 'connecting'};
const dictionary = new Node(); dictionary.textContent = process.argv[3];
const listeners = {}; let connection, closed = false, interval;
class EventSource {
  constructor() { connection = this; }
  addEventListener(type, fn) { listeners[type] = fn; }
  close() { closed = true; }
}
const document = {
  querySelector(selector) {
    queries++; if (selector.includes('[data-phase]')) phaseQueries++;
    return selector === '[data-run-id]' ? progress : null;
  },
  querySelectorAll(selector) {
    queries++; if (selector.includes('[data-phase]')) phaseQueries++;
    return selector === '[data-step-row]' ? rows.map(row => row.node) : [];
  },
  getElementById(id) {
    queries++; return id === 'pipeline-wording' ? dictionary : nodes[id] ?? null;
  },
  createElement: tag => new Node(tag),
};
let now = Date.parse('2026-10-03T15:02:00Z');
class Clock extends Date { static now() { return now; } }
const context = {document, EventSource, Date: Clock};
if (scenario === 'clock') context.window = {setInterval: fn => { interval = fn; return 1; },
  clearInterval() {}, addEventListener() {}};
vm.runInNewContext(source, context);
let seq = 0;
const send = (type, fields = {}) => {
  const count = phaseQueries;
  listeners[type]({data: JSON.stringify({type, seq: ++seq, ts: first, ...fields})});
  assert.ok(phaseQueries - count <= 1, 'at most one phase lookup per step');
};
const has = (node, name) => node.classList.contains(name);
if (scenario === 'icons') {
  const before = queries;
  for (const [state, kind, label] of [['running','running','진행 중'],
      ['succeeded','success','완료'], ['failed','failure','실패'],
      ['check_failed','failure','검사 불합격'], ['waiting','waiting','대기'],
      ['skipped','neutral','건너뜀'], ['unrecorded','neutral','기록 없음']]) {
    send(state === 'running' ? 'step.started' : 'step.finished',
      {step: 'verify.health.local', target: 'local', status: state});
    assert.equal(local.badge.children.length, 2); assert.equal(local.badge.children[0], local.icon);
    assert.equal(local.icon.getAttribute('aria-hidden'), 'true');
    assert.equal(local.label.textContent, label);
    assert.ok(has(local.badge, kind)); assert.equal(local.badge.dataset.code, state);
    assert.equal(local.detail.open, state !== 'succeeded');
  }
  assert.equal(queries, before, 'cached row updates need no document queries');
  send('step.started', {step: 'new.task', target: 'cloud'});
  send('step.finished', {step: 'new.task', target: 'cloud', status: 'succeeded'});
  assert.equal(queries, before, 'dynamic rows also need no document queries');
  const dynamic = nodes['pipeline-cloud'].children[0];
  assert.equal(dynamic.querySelector('[data-step-detail]').open, false);
  assert.equal(dynamic.querySelector('[data-step-status]').children[0].tagName, 'I');
  assert.ok(dynamic.querySelector('[data-step-status]').textContent.includes('완료'));
} else if (scenario === 'clock') {
  assert.equal(nodes['local-work-age'].textContent, '환경 시작 후 2분 0초');
  assert.equal(nodes['current-step-clock'].textContent, '30초');
  send('step.finished', {step: 'verify.health.local', target: 'local', status: 'succeeded'});
  send('step.started', {step: 'deploy.was.local', target: 'local', ts: '2026-10-03T15:02:00Z'});
  assert.equal(nodes['local-work-age'].dataset.laneStarted, first);
  assert.equal(nodes['current-step-clock'].textContent, '0초');
  now += 1000; interval();
  assert.equal(nodes['local-work-age'].textContent, '환경 시작 후 2분 1초');
  assert.equal(nodes['current-step-clock'].textContent, '1초');
  send('gate.waiting', {step: 'deploy.infra.cloud', target: 'cloud', ts: '2026-10-03T14:59:00Z'});
  assert.equal(nodes['cloud-work-age'].dataset.laneStarted, '');
  send('step.started', {step: 'deploy.infra.cloud', target: 'cloud', ts: '2026-10-03T15:01:00Z'});
  assert.equal(nodes['cloud-work-age'].textContent, '환경 시작 후 1분 1초');
  send('step.started', {step: 'deploy.was.cloud', target: 'cloud', ts: first});
  assert.equal(nodes['cloud-work-age'].dataset.laneStarted, first);
  send('step.started', {step: 'verify.health.cloud', target: 'cloud', ts: 'invalid'});
  assert.equal(nodes['cloud-work-age'].dataset.laneStarted, first);
} else if (scenario === 'warning') {
  send('step.started', {step: 'deploy.infra.cloud', target: 'cloud'});
  assert.ok(has(infra.title, 'failure')); assert.ok(has(nodes['activity-title'], 'failure'));
  assert.ok(infra.title.textContent.includes('S3 이미지 저장소 삭제'));
  send('step.finished', {step: 'deploy.infra.cloud', target: 'cloud', status: 'succeeded'});
  assert.ok(has(infra.title, 'failure')); assert.ok(!has(nodes['activity-title'], 'failure'));
  assert.equal(infra.detail.open, false); assert.equal(infra.outcome.hidden, false);
  assert.ok(!infra.outcome.textContent.includes('(변경 '), 'SSE has no counts');
  infra.sentence.textContent += ' (변경 3)';
  send('step.finished', {step: 'deploy.infra.cloud', target: 'cloud', status: 'succeeded'});
  assert.ok(infra.outcome.textContent.endsWith('(변경 3)'), 'preserve recorded server counts');
} else if (scenario === 'connection') {
  connection.onopen();
  assert.ok(has(local.node, 'is-running')); assert.ok(has(nodes['now-working'], 'is-working'));
  send('step.started', {step: 'deploy.infra.cloud', target: 'cloud'});
  connection.onerror();
  assert.equal(progress.dataset.stream, 'reconnecting');
  assert.ok(!has(local.node, 'is-running')); assert.ok(!has(infra.node, 'is-running'));
  assert.ok(!has(nodes['now-working'], 'is-working'));
  assert.equal(local.node.dataset.stepState, 'running');
  connection.onopen();
  assert.ok(has(local.node, 'is-running')); assert.ok(has(infra.node, 'is-running'));
  assert.ok(!has(earlier.node, 'is-running'));
  send('step.finished', {step: 'verify.health.local', target: 'local', status: 'succeeded'});
  connection.onerror(); connection.onopen();
  assert.ok(!has(local.node, 'is-running')); assert.ok(has(infra.node, 'is-running'));
  send('run.state', {status: 'FAILED_CLOUD'});
  connection.onerror(); connection.onopen();
  assert.ok(closed); assert.equal(progress.dataset.activity, 'ended');
  assert.ok(!has(infra.node, 'is-running')); assert.ok(!has(nodes['now-working'], 'is-working'));
} else if (scenario === 'replay') {
  send('step.started', {step: 'deploy.infra.cloud', target: 'cloud'});
  send('step.finished', {step: 'deploy.infra.cloud', target: 'cloud', status: 'succeeded'});
  assert.ok(infra.outcome.textContent.endsWith('(변경 3)'), 'full replay preserves server counts');
  send('step.started', {step: 'deploy.infra.cloud', target: 'cloud', ts: '2026-10-03T16:00:00Z'});
  send('step.finished', {step: 'deploy.infra.cloud', target: 'cloud', status: 'succeeded'});
  assert.ok(!infra.outcome.textContent.includes('(변경 '), 'new attempt cannot inherit old counts');
} else if (scenario === 'time') {
  for (const [ts, expected] of [['2026-10-03T15:00:00Z','00:00:00'],
    ['2026-10-03T16:02:03Z','01:02:03'], ['2026-10-03T14:59:59Z','23:59:59'],
    ['bad','시각 없음']]) {
    send('step.started', {step: 'verify.health.local', target: 'local', ts});
    assert.ok(nodes['local-events'].children.at(-1).textContent.startsWith(expected + ' · '));
  }
}
"""


@pytest.mark.parametrize("scenario", ["icons", "clock", "warning", "connection", "replay", "time"])
def test_progress_sse_updates_without_external_services(scenario):
    package = distribution("nodejs-wheel-binaries")
    node = next(package.locate_file(f) for f in package.files or () if str(f).endswith("/bin/node"))
    dictionary = {
        "texts": {
            "verify.health.local": wording("verify.health.local"),
            "deploy.infra.cloud": wording(
                "deploy.infra.cloud", storage={"intent": "remove", "bucket": "fixture-images"}
            ),
        }
    }
    result = subprocess.run(
        [str(node), "-e", SCRIPT, str(WEB / "static/app.js"), scenario, json.dumps(dictionary)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def render_progress(pipeline):
    env = Environment(loader=FileSystemLoader(WEB / "templates"), autoescape=select_autoescape())
    return env.get_template("progress.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/runs/fixture")),
        run_id="fixture",
        project="fixture",
        status="RUNNING",
        targets="both",
        terminal_states=["SUCCEEDED"],
        pipeline={**pipeline, "started": "2026-10-03T15:00:00Z", "remaining": 1},
        story={"compare": {"available": False}},
        result={},
        environment_cards=[],
        cards_version="fixture",
        narrative_errors={},
    )


@pytest.mark.parametrize("counts,expected", [(None, None), ({"create": 0}, 0), ({"delete": 3}, 3)])
def test_progress_completed_row_is_one_summary_with_only_recorded_counts(counts, expected):
    output = {"counts": counts} if counts is not None else {}
    pipeline = pipeline_view(
        {
            "status": "RUNNING",
            "result": {
                "steps": {
                    "deploy.infra.cloud": {
                        "tool": "apply_infra",
                        "status": "succeeded",
                        "elapsed_s": 3,
                        "output": output,
                    }
                }
            },
        },
        {},
        [
            {
                "type": "step.started",
                "step": "deploy.infra.cloud",
                "target": "cloud",
                "ts": "2026-10-03T15:00:00Z",
            }
        ],
        storage={"intent": "remove", "bucket": "fixture-images"},
    )
    html = render_progress(pipeline)
    row = re.search(r'<li class="pipeline-step complete"[^>]*>(.*?)</li>', html, re.S)[1]
    assert row.startswith("<details data-step-detail><summary")
    summary = row.split("</summary>", 1)[0]
    assert "data-step-title" in summary and "data-step-outcome" in summary
    assert 'class="state failure"' in summary and "S3 이미지 저장소 삭제" in summary
    assert '<i aria-hidden="true"></i><span>완료</span>' in summary
    assert 'class="step-meta"' in row.split("</summary>", 1)[1]
    assert row.endswith("</details>")
    if expected is None:
        assert "(변경 " not in summary
    else:
        assert f"(변경 {expected})" in summary


def test_progress_render_keeps_first_lane_start_separate_from_current_step():
    events = [
        {
            "type": "step.started",
            "step": "deploy.was.local",
            "target": "local",
            "ts": "2026-10-03T15:00:00Z",
        },
        {
            "type": "step.finished",
            "step": "deploy.was.local",
            "target": "local",
            "status": "succeeded",
            "elapsed_s": 30,
        },
        {
            "type": "step.started",
            "step": "verify.health.local",
            "target": "local",
            "ts": "2026-10-03T15:01:30Z",
        },
    ]
    html = render_progress(pipeline_view({"status": "RUNNING", "result": {}}, {}, events))
    assert 'data-lane-started="2026-10-03T15:00:00Z"' in html
    assert 'data-step-state="running" data-started="2026-10-03T15:01:30Z"' in html
    assert "<details data-step-detail open><summary" in html


def test_reconnecting_css_pauses_all_progress_animations():
    css = (WEB / "static/app.css").read_text()
    pause = re.search(
        r'(\.progress\[data-stream="reconnecting"\][^{]+)\{\s*animation-play-state: paused;', css
    )[1]
    for selector in (".activity-dots i", ".pipeline-step::after", ".state.running i"):
        assert selector in pause
    assert ".pipeline-step.complete [data-step-summary] { display: grid;" in css
    assert ".pipeline-step.complete [data-step-outcome] { overflow: hidden;" in css
