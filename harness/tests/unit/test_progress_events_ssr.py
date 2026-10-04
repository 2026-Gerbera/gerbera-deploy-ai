"""source=fixture: 종료 화면 기록 복원과 기존 SSE 표시의 일치. 외부 호출 없음."""

import json
import re
import subprocess
from html import unescape
from importlib.metadata import distribution

import pytest

from ddak.core.redact import redact_obj
from ddak.web.i18n import translate_tree
from ddak.web.js_wording import wording
from ddak.web.narrative import pipeline_view
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_ui_integration_fix10 import client_for, prepare
from tests.unit.test_ui_progress_followup import SCRIPT, WEB

PRIVATE = "fixture-" + "hidden-value"
EVENTS = [
    {"type": "run.state", "status": "RUNNING"},
    {"type": "stage.finished", "preparation_stage": "analyze", "status": "succeeded"},
    {"type": "step.started", "step": "deploy.was.local"},
    {"type": "step.finished", "step": "deploy.was.local", "status": "succeeded", "elapsed_s": 65.9},
    {"type": "gate.opened", "target": "local", "detail": "local_verified"},
    {"type": "step.started", "step": "deploy.was.cloud"},
    {"type": "gate.waiting", "target": "cloud", "detail": f"password={PRIVATE} <b>gate</b>"},
    {"type": "gate.failed", "target": "cloud", "detail": "images_ready"},
    {"type": "step.finished", "step": "deploy.was.cloud", "status": "failed", "detail": PRIVATE},
    {"type": "rollback.started", "target": "cloud"},
    {"type": "rollback.finished", "target": "cloud", "status": "succeeded", "elapsed_s": 1.9},
    {"type": "step.skipped", "step": "verify.compare", "status": "skipped"},
    {"type": "ai.call", "step": "analyze", "detail": PRIVATE},
    {"type": "report.ready", "detail": PRIVATE},
]


def record(rig, status):
    service, source, _ = rig
    rid = prepare(service, source)
    service.approve(rid, approver="fixture")
    service.store.acquire(service.get_run(rid)["project"], rid)
    events = [
        {"run_id": rid, "seq": i, "ts": "2026-10-03T15:00:00Z", **event}
        for i, event in enumerate(EVENTS)
    ]
    if status != "RUNNING":
        events.append(
            {
                "run_id": rid,
                "seq": len(events),
                "ts": "2026-10-03T15:01:06Z",
                "type": "run.state",
                "status": status,
            }
        )
        service.store.finish(rid, status, {"steps": {}, "tracks": {}}, {}, {})
    (service.root / "runs" / rid / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events)
    )
    return service, rid, events


def lists(html):
    return {
        track: re.search(rf'<ol id="{track}-events" class="event-list">(.*?)</ol>', html, re.S)[1]
        for track in ("local", "cloud", "common")
    }


@pytest.mark.parametrize("status", ("SUCCEEDED", "FAILED_CLOUD", "NEEDS_HUMAN"))
@pytest.mark.parametrize("lang", ("ko", "ja"))
def test_terminal_progress_restores_three_lists_on_each_request(rig, status, lang):
    service, rid, events = record(rig, status)
    with client_for(service) as client:
        response = client.get(f"/runs/{rid}/progress?lang={lang}")
        refreshed = client.get(f"/runs/{rid}/progress?lang={lang}")
    assert response.status_code == refreshed.status_code == 200
    assert f'<html lang="{lang}">' in response.text
    history = lists(response.text)
    assert lists(refreshed.text) == history
    assert all('<li data-seq="' in content for content in history.values())
    sequences = [
        int(seq) for content in history.values() for seq in re.findall(r'data-seq="(\d+)"', content)
    ]
    assert sorted(sequences) == list(range(len(events)))
    assert "00:00:00 · " in history["local"]
    assert ("1분 5초" if lang == "ko" else "1分5秒") in history["local"]
    assert ("복구 시작" if lang == "ko" else wording(lang)["event.rollback_started"]) in history[
        "cloud"
    ]
    assert "local_verified" in history["local"] and "images_ready" in history["cloud"]
    assert "&lt;b&gt;gate&lt;/b&gt;" in history["cloud"] and "[REDACTED]" in history["cloud"]
    assert PRIVATE not in response.text


@pytest.mark.parametrize("lang", ("ko", "ja"))
def test_running_progress_keeps_lists_empty_for_sse(rig, lang):
    service, rid, _ = record(rig, "RUNNING")
    with client_for(service) as client:
        response = client.get(f"/runs/{rid}/progress?lang={lang}")
    assert response.status_code == 200
    assert lists(response.text) == {"local": "", "cloud": "", "common": ""}
    assert 'data-status="RUNNING"' in response.text


@pytest.mark.parametrize("lang", ("ko", "ja"))
def test_server_history_matches_actual_sse_item_text_and_track(rig, lang):
    service, rid, events = record(rig, "FAILED_CLOUD")
    with client_for(service) as client:
        response = client.get(f"/runs/{rid}/progress?lang={lang}")
    history = {
        track: [
            unescape(re.sub(r"<[^>]*>", "", item))
            for item in re.findall(r"<li[^>]*>(.*?)</li>", content)
        ]
        for track, content in lists(response.text).items()
    }
    pipeline = pipeline_view(service.get_run(rid), service.get_display_data(rid)["plan"], events)
    dictionary = {key: pipeline[key] for key in ("texts", "aliases", "gates", "errors")}
    if lang == "ja":
        dictionary = translate_tree(dictionary)
    # 기존 DOM 대역에 실제 app.js와 같은 이벤트를 넣어 문자열·분류를 비교한다.
    script = (
        SCRIPT.split("vm.runInNewContext(source, context);")[0]
        + """
nodes['app-wording'] = new Node(); nodes['app-wording'].textContent = process.argv[5];
vm.runInNewContext(source, context);
for (const event of JSON.parse(process.argv[4])) {
  listeners[event.type]({data: JSON.stringify(event)});
}
console.log(JSON.stringify(Object.fromEntries(['local', 'cloud', 'common'].map(track =>
  [track, nodes[`${track}-events`].children.map(item => item.textContent)]))));
"""
    )
    package = distribution("nodejs-wheel-binaries")
    node = next(package.locate_file(f) for f in package.files or () if str(f).endswith("/bin/node"))
    result = subprocess.run(
        [
            str(node),
            "-e",
            script,
            str(WEB / "static/app.js"),
            "history",
            json.dumps(dictionary),
            json.dumps(redact_obj(events)),
            json.dumps(wording(lang)),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert history == json.loads(result.stdout)
