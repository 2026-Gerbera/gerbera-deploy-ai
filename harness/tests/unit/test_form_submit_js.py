"""source=fixture: 실제 통신 없이 관리 POST 폼의 브라우저 제출 동작을 검증한다."""

import json
import subprocess
from importlib.metadata import distribution
from pathlib import Path

import pytest

from ddak.web.narrative import ERRORS

SCRIPT = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];
const errors = JSON.parse(process.argv[3]); // base.html narrative-errors와 같은 서술 사전
const FALLBACK = '작업을 완료하지 못했습니다. 기술 정보에서 원인을 확인하세요.';
class Node {
  constructor(text = '') {
    this.textContent = text;
    this.hidden = true;
    this.attrs = new Map();
    this.classes = new Set();
    this.classList = {add: (...names) => names.forEach(name => this.classes.add(name))};
  }
  set innerHTML(_) { throw Error('HTML insertion is forbidden'); }
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  setAttribute(name, value) { this.attrs.set(name, value); }
  removeAttribute(name) { this.attrs.delete(name); }
  hasAttribute(name) { return this.attrs.has(name); }
  matches(selector) {
    return selector === '[data-form-error]' && this.hasAttribute('data-form-error');
  }
}
function errorBox() {
  const box = new Node('⚠ 오류');
  box.setAttribute('data-form-error', '');
  box.setAttribute('role', 'alert');
  box.classList.add('notice', 'failure');
  box.code = new Node('OLD_CODE');
  box.message = new Node('old server flash');
  box.detail = new Node('old server detail');
  box.querySelector = selector => ({
    '[data-error-code]': box.code, '[data-error-message]': box.message,
    '[data-error-detail]': box.detail,
  })[selector] ?? null;
  box.cloneNode = deep => { assert.equal(deep, true); return errorBox(); };
  return box;
}
function field(name, value, type = 'text', disabled = false) {
  return Object.assign(new Node('버튼 원래 표시'), {name, value, type, disabled});
}
function form(action, dataset = {}) {
  const f = Object.assign(new Node(), {action, method: 'post', dataset, listeners: []});
  f.setAttribute('action', action);
  for (const key of Object.keys(dataset)) {
    f.setAttribute('data-' + key.replace(/[A-Z]/g, c => '-' + c.toLowerCase()), dataset[key]);
  }
  f.elements = [
    field('csrf_token', 'fixture-csrf', 'hidden'),
    field('_form_id', action, 'hidden'), field('_return_to', '/setup?project=fixture', 'hidden'),
    field('project', 'fixture'), field('value', 'fixture-' + 'private-value', 'password'),
    field('inventory', '{"label":"한글 & + 값"}', 'textarea'),
    field('targets', 'cloud', 'checkbox'), field('targets', 'onprem', 'checkbox'),
    field('ignored', 'do-not-send', 'text', true),
    field('decision', 'approved', 'submit'), field('decision', 'denied', 'submit'),
    field('unavailable', 'disabled', 'submit', true),
  ];
  f.buttons = f.elements.filter(el => el.type === 'submit');
  f.buttons[2].setAttribute('aria-busy', 'false');
  f.nextElementSibling = errorBox();
  f.nextElementSibling.replaceWith = box => { f.nextElementSibling = box; };
  f.querySelector = selector => selector === '[name="_form_id"]'
    ? f.elements.find(el => el.name === '_form_id') : null;
  f.addEventListener = (name, fn) => {
    if (name === 'submit') f.listeners.push(fn);
    else { assert.ok(['input', 'change'].includes(name)); f[name] = fn; }
  };
  f.insertAdjacentElement = (position, node) => {
    assert.equal(position, 'afterend'); f.nextElementSibling = node;
  };
  f.emit = (submitter = f.buttons[0], prevented = false) => {
    const event = {submitter, defaultPrevented: prevented,
      preventDefault() { this.defaultPrevented = true; }};
    return {event, done: Promise.all(f.listeners.map(fn => fn(event)))};
  };
  return f;
}
class FormData {
  constructor(f) {
    this.entries = f.elements.filter(el => el.name && !el.disabled && el.type !== 'submit')
      .map(el => [el.name, el.value]);
  }
  [Symbol.iterator]() { return this.entries[Symbol.iterator](); }
}
const forms = [form('/setup/docker'), form('/settings'),
  form('/ops/plan', {deployForm: ''}), form('/runs/fixture/approval', {approvalForm: ''}),
  form('/setup/actions/apply'), form('/ops/unlock', {confirm: '잠금을 풀까요?'}),
  form('/setup/env'), form('/setup/git-token'), form('/runs/fixture/patch-review')];
const calls = [], navigations = [], confirmations = [];
let reloads = 0, confirmResult = true;
let respond = async () => ({ok: false, status: 422, json: async () => ({error: {
  code: 'VALIDATION_ERROR', message: '<img src=x onerror=bad()> 서버 검증 실패',
}})});
const loading = new Node();
const dockerBadge = new Node();
dockerBadge.dataset = {status: 'gray'};
const dockerLabel = new Node('미확인'), dockerDetail = new Node('실제 연결 확인 전');
dockerBadge.querySelector = selector => selector === 'span' ? dockerLabel : null;
const dockerCheck = {querySelector: selector => ({
  '[data-status]': dockerBadge, '.section-description': dockerDetail,
})[selector] ?? null};
const template = {content: {firstElementChild: errorBox()}};
const document = {
  querySelectorAll(selector) {
    if (selector === 'form') return forms;
    if (selector === '[data-confirm], [data-approval-form]') {
      return forms.filter(f => 'confirm' in f.dataset || 'approvalForm' in f.dataset);
    }
    return [];
  },
  querySelector: selector => ({
    '[data-deploy-loading]': loading, '[data-check="docker"]': dockerCheck,
  })[selector] ?? null,
  getElementById: id => id === 'form-error-template' ? template
    : id === 'narrative-errors' ? {textContent: process.argv[3]} : null,
};
const window = {
  FormData, URLSearchParams, URL,
  location: {href: 'https://admin.example/setup?project=fixture',
    origin: 'https://admin.example', search: '?project=fixture',
    assign: url => navigations.push(url), reload: () => reloads++},
  confirm: message => { confirmations.push(message); return confirmResult; },
  fetch: (url, options) => { calls.push({url, options}); return respond(); },
};
function restored(f) {
  assert.deepEqual(f.buttons.map(b => b.disabled), [false, false, true]);
  assert.deepEqual(f.buttons.map(b => b.getAttribute('aria-busy')), [null, null, 'false']);
  assert.ok(f.buttons.every(b => b.textContent === '버튼 원래 표시'));
  assert.equal(loading.hidden, true);
}
function noNavigation() {
  assert.equal(navigations.length, 0); assert.equal(reloads, 0);
}
function genericError(f) {
  assert.equal(f.nextElementSibling.code.textContent, 'REQUEST_FAILED');
  // 사람용 문구는 서술 사전에서, 원문은 기술 정보 칸에만 둔다.
  assert.equal(f.nextElementSibling.message.textContent, errors.REQUEST_FAILED);
  assert.equal(f.nextElementSibling.detail.textContent,
    '요청을 처리하지 못했습니다. 다시 시도해 주세요.');
  assert.equal(f.nextElementSibling.hidden, false);
  restored(f); noNavigation();
}
async function run() {
  if (scenario === 'no-window') {
    vm.runInNewContext(source, {document});
    assert.ok(forms.every(f => f.listeners.length === 0));
    return;
  }
  if (scenario === 'polling' || scenario === 'polling-input') {
    const timeouts = new Map(), feedback = []; let timerId = 0, replacements = 0;
    const originalQuery = document.querySelector;
    let active = forms[0], region;
    const makeRegion = (version, f) => ({dataset: {liveVersion: version},
      contains: () => false, querySelectorAll: () => f ? [f] : [],
      replaceWith(next) { region = next; replacements++; }});
    region = makeRegion('one', active);
    document.querySelector = selector => selector === '[data-live-region]' ? region
      : selector === '[data-refresh-errors]' ? {append: box => feedback.push(box)}
      : originalQuery(selector);
    document.importNode = node => {
      const f = node.querySelectorAll('form')[0];
      if (f) forms[0] = f; else forms.splice(0,1);
      return node;
    };
    window.setTimeout = fn => { timeouts.set(++timerId, fn); return timerId; };
    window.clearTimeout = id => timeouts.delete(id);
    window.setInterval = () => 1; window.clearInterval = () => {};
    const replacementForm = form('/setup/docker');
    let next = makeRegion('two', replacementForm);
    class DOMParser { parseFromString() { return {querySelector: selector =>
      selector === '[data-live-region]' ? next : null}; } }
    const fetch = async () => ({ok: true, text: async () => '<html/>'});
    vm.runInNewContext(source, {document, window, DOMParser, fetch, AbortController});
    const tick = async () => {
      const [key, fn] = [...timeouts][0]; timeouts.delete(key); await fn(); };
    if (scenario === 'polling-input') {
      active.input(); await tick(); assert.equal(replacements, 0); return;
    }
    let release;
    respond = () => new Promise(resolve => { release = resolve; });
    const pending = active.emit();
    await tick(); assert.equal(replacements, 0);
    release({ok: false, json: async () => ({error: {code: 'FAILED', message: '폼 오류'}})});
    await pending.done;
    const error = active.nextElementSibling;
    await tick(); assert.equal(replacements, 1);
    assert.equal(replacementForm.nextElementSibling, error); // still directly below its form
    assert.equal(error.hidden, false); assert.equal(error.code.textContent, 'FAILED');
    assert.equal(replacementForm.listeners.length, 1);
    await tick(); assert.equal(replacementForm.listeners.length, 1);
    const before = calls.length, retry = replacementForm.emit();
    assert.equal(calls.length, before + 1);
    release({ok: false, json: async () => ({error: {code: 'FAILED', message: '새 폼 오류'}})});
    await retry.done; restored(replacementForm);
    next = makeRegion('approval-ready', null); // server accepted request despite lost response
    await tick(); assert.equal(replacements, 2);
    assert.equal(region.dataset.liveVersion, 'approval-ready');
    assert.equal(feedback.length, 1); assert.equal(feedback[0], error);
    noNavigation(); return;
  }
  vm.runInNewContext(source, {document, window});
  assert.ok(forms.every(f => f.listeners.length === 1));
  const f = forms[2];
  if (scenario === 'failures') {
    forms[0].nextElementSibling = null; // clone fallback
    forms[1].nextElementSibling.hidden = false; // existing server flash
    for (const current of forms) {
      const before = current.elements.map(el => [el.name, el.value]);
      const oldBox = current.nextElementSibling;
      const otherBoxes = forms.filter(other => other !== current).map(other =>
        [other, other.nextElementSibling, other.nextElementSibling?.hidden]);
      const submission = current.emit();
      assert.equal(submission.event.defaultPrevented, true);
      await submission.done;
      assert.deepEqual(current.elements.map(el => [el.name, el.value]), before);
      const box = current.nextElementSibling;
      if (oldBox) assert.equal(box, oldBox);
      assert.ok(box.matches('[data-form-error]') && box.classes.has('notice'));
      assert.ok(box.classes.has('failure')); assert.equal(box.getAttribute('role'), 'alert');
      assert.equal(box.textContent, '⚠ 오류'); assert.equal(box.hidden, false);
      assert.equal(box.code.textContent, 'VALIDATION_ERROR');
      assert.equal('VALIDATION_ERROR' in errors, false);
      assert.equal(box.message.textContent, FALLBACK);
      assert.equal(box.detail.textContent, '<img src=x onerror=bad()> 서버 검증 실패');
      otherBoxes.forEach(([other, node, hidden]) => {
        assert.equal(other.nextElementSibling, node);
        assert.equal(other.nextElementSibling?.hidden, hidden);
      });
      restored(current);
      const {url, options} = calls.at(-1);
      assert.equal(url, 'https://admin.example' + current.action);
      assert.equal(options.method, 'POST'); assert.equal(options.credentials, 'same-origin');
      assert.equal(options.headers['X-Ddak-Form'], '1');
      assert.equal(options.headers.Accept, 'text/html');
      assert.equal(options.headers['Content-Type'], 'application/x-www-form-urlencoded');
      assert.equal('Origin' in options.headers, false);
      const body = new URLSearchParams(options.body);
      for (const [name, value] of before.filter(([name]) =>
        !['targets', 'ignored', 'decision', 'unavailable'].includes(name))) {
        assert.equal(body.get(name), value);
      }
      assert.deepEqual(body.getAll('targets'), ['cloud', 'onprem']);
      assert.deepEqual(body.getAll('decision'), ['approved']);
      assert.equal(body.has('ignored'), false); assert.equal(body.has('unavailable'), false);
    }
    assert.equal(calls.length, forms.length); noNavigation();
    assert.equal(dockerBadge.dataset.status, 'red');
    assert.equal(dockerLabel.textContent, '확인 필요');
    assert.equal(dockerDetail.textContent, '<img src=x onerror=bad()> 서버 검증 실패');
  } else if (scenario === 'named-action') {
    const patch = forms.at(-1);
    patch.action = {toString: () => '[object RadioNodeList]'};
    patch.buttons[0].name = 'action'; patch.buttons[0].value = 'revise:cookie';
    patch.elements.push(field('revision', '7', 'hidden'),
      field('candidate_id', 'candidate-1', 'hidden'));
    await patch.emit().done;
    assert.equal(calls[0].url, 'https://admin.example/runs/fixture/patch-review');
    const body = new URLSearchParams(calls[0].options.body);
    assert.equal(body.get('action'), 'revise:cookie');
    assert.equal(body.get('revision'), '7'); assert.equal(body.get('candidate_id'), 'candidate-1');
    restored(patch);
  } else if (scenario === 'denied') {
    await forms[3].emit(forms[3].buttons[1]).done;
    assert.equal(confirmations.length, 1); assert.match(confirmations[0], /거절/);
    assert.deepEqual(new URLSearchParams(calls[0].options.body).getAll('decision'), ['denied']);
    restored(forms[3]); noNavigation();
  } else if (scenario === 'cancel') {
    confirmResult = false;
    f.dataset.confirm = '배포할까요?';
    for (const current of [f, forms[3], forms[5]]) {
      const submission = current.emit(current.buttons[1]);
      await submission.done;
      assert.equal(submission.event.defaultPrevented, true); restored(current);
    }
    assert.equal(confirmations.length, 3); assert.equal(calls.length, 0); noNavigation();
  } else if (scenario === 'redirect' || scenario === 'reload') {
    const url = 'https://admin.example/runs/fixture/approval?project=fixture';
    respond = async () => ({ok: true, status: 200, redirected: scenario === 'redirect', url});
    await f.emit().done;
    assert.deepEqual(navigations, scenario === 'redirect' ? [url] : []);
    assert.equal(reloads, scenario === 'reload' ? 1 : 0);
  } else if (scenario === 'duplicate') {
    let resolve;
    respond = () => new Promise(done => { resolve = done; });
    const first = f.emit();
    assert.ok(f.buttons.every(b => b.disabled && b.getAttribute('aria-busy') === 'true'));
    assert.equal(loading.hidden, false);
    const second = f.emit(); await second.done;
    assert.equal(second.event.defaultPrevented, true); assert.equal(calls.length, 1);
    resolve({ok: false, json: async () => ({error: {code: 'BUSY', message: '다시 시도'}})});
    await first.done; restored(f);
    const retry = f.emit(); assert.equal(calls.length, 2);
    resolve({ok: true, redirected: false}); await retry.done;
  } else if (scenario === 'get' || scenario === 'prevented') {
    if (scenario === 'get') f.method = 'get';
    const submission = f.emit(f.buttons[0], scenario === 'prevented');
    await submission.done; assert.equal(calls.length, 0); restored(f); noNavigation();
  } else if (scenario === 'enter') {
    await f.emit(null).done;
    assert.equal(new URLSearchParams(calls[0].options.body).has('decision'), false);
    restored(f);
  } else {
    const raw = 'sensitive-' + 'raw-response';
    if (scenario === 'network' || scenario === 'abort') {
      respond = async () => { const error = Error(raw);
        error.name = scenario === 'abort' ? 'AbortError' : 'TypeError'; throw error; };
    } else if (scenario === 'non-json') {
      respond = async () => ({ok: false, json: async () => { throw Error(raw); }});
    } else if (scenario === 'malformed') {
      respond = async () => ({ok: false, json: async () => ({error: {code: {}, message: raw}})});
    } else if (scenario === 'cross-origin') {
      respond = async () => ({ok: true, redirected: true, url: 'https://other.example/' + raw});
    } else if (scenario === 'cross-action') {
      f.setAttribute('action', 'https://other.example/post');
    } else throw Error('unknown scenario');
    await f.emit().done; genericError(f);
    if (scenario === 'cross-action') assert.equal(calls.length, 0);
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize(
    "scenario",
    [
        "failures",
        "denied",
        "cancel",
        "redirect",
        "reload",
        "duplicate",
        "network",
        "abort",
        "non-json",
        "malformed",
        "cross-origin",
        "cross-action",
        "get",
        "prevented",
        "enter",
        "no-window",
        "polling",
        "polling-input",
        "named-action",
    ],
)
def test_management_form_submission(scenario):
    package = distribution("nodejs-wheel-binaries")
    node = next(package.locate_file(f) for f in package.files or () if str(f).endswith("/bin/node"))
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            str(node),
            "-e",
            SCRIPT,
            str(root / "src/ddak/web/static/app.js"),
            scenario,
            json.dumps(ERRORS, ensure_ascii=False),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
