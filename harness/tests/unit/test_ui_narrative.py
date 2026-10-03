"""source=fixture: 단계 서술·본문 비노출·색 대비를 확인한다."""

import json
import re
from html.parser import HTMLParser
from pathlib import Path

from ddak.core.contracts.step_catalog import catalog_steps
from ddak.core.registry import CATALOG
from ddak.web.narrative import TEXT, UNKNOWN, pipeline_view, planned_rows, wording
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_ui_integration_fix10 import client_for, prepare


class BodyText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden_depth = 0
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in {"details", "script", "style"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag in {"details", "script", "style"}:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth:
            self.text.append(data)


def body(html):
    parser = BodyText()
    parser.feed(html)
    return " ".join(parser.text)


def test_dictionary_covers_canonical_catalog_and_noncanonical_steps():
    assert {s.name for s in CATALOG if s.canonical} <= TEXT.keys()
    for step in catalog_steps(["was", "web", "db", "worker"], "both"):
        text = wording(step.id, step.tool)
        assert text != UNKNOWN
        assert all(text[key] for key in ("name", "description", "running", "finished", "failed"))
    for step in (
        "intake",
        "detect",
        "analyze",
        "plan",
        "validate",
        "source",
        "prepare",
        "verify.diagnose",
        "prepare.infra.cloud",
        "rollback.local",
        "deploy.migrate.local",
        "check_patch",
        "gitleaks",
    ):
        assert wording(step) != UNKNOWN


def test_independent_lanes_use_recorded_state_and_never_expose_output_values():
    secret = "private-fixture" + "-narrative"
    plan = {
        "deploy": {
            "local": {"steps": [{"id": "verify.health.local", "tool": "health_check"}]},
            "cloud": {"steps": [{"id": "deploy.was.cloud", "tool": "deploy_tier"}]},
        }
    }
    events = [
        {
            "type": "step.started",
            "step": "verify.health.local",
            "target": "local",
            "ts": "2026-10-03T10:00:00Z",
        },
        {
            "type": "step.finished",
            "step": "verify.health.local",
            "target": "local",
            "status": "succeeded",
            "elapsed_s": 3,
        },
        {"type": "step.started", "step": "deploy.was.cloud", "target": "cloud"},
    ]
    run = {
        "status": "RUNNING",
        "result": {
            "steps": {
                "verify.smoke.local": {
                    "tool": "smoke_test",
                    "status": "succeeded",
                    "elapsed_s": 2,
                    "output": {
                        "scenarios": [{"ok": True, "normalized": {"password": secret}}],
                        "detail": secret,
                    },
                },
            }
        },
    }
    view = pipeline_view(run, plan, events)
    assert view["lanes"]["local"][0]["status"] == "succeeded"
    assert view["lanes"]["cloud"][0]["status"] == "running"
    assert view["lanes"]["local"][-1]["sentence"] == "사용자 시나리오 1개 중 1개 통과."
    assert secret not in json.dumps(view)
    run["status"] = "FAILED_CLOUD"
    ended = pipeline_view(run, plan, events)
    assert ended["lanes"]["cloud"][0]["status"] == "unrecorded"
    assert run["status"] == "FAILED_CLOUD"


def test_plan_reuse_reason_and_backend_are_readable():
    plan = {
        "build": {
            "steps": [{"id": "build.was", "tool": "build_image"}],
            "skipped": [{"id": "build.web", "tool": "build_image", "skip_rule": "tree_unchanged"}],
        }
    }
    rows = planned_rows(plan, "local")
    assert "이전 이미지를 재사용" in rows[1]["why"]
    assert "온프레미스 로컬 빌드" in rows[0]["running"]
    assert "CodeBuild" in wording("build.was", backend="codebuild")["running"]


def test_http_narrative_keeps_alias_approval_and_masks_raw_errors(rig, monkeypatch):
    service, source, _ = rig
    monkeypatch.setenv("DDAK_WATCH_PROJECT", "flaskr-three")
    rid = prepare(service, source)
    with client_for(service) as client:
        home = client.get("/").text
        assert "<h1>flaskr-three</h1>" in home and 'data-url="/code-qa/ask"' in home
        assert "<h1>전체 프로젝트</h1>" in client.get("/projects").text
        approval = client.get(f"/runs/{rid}/approval").text
        assert approval == client.get(f"/ops/runs/{rid}/approval").text
        assert 'id="deployment-approval"' in approval and approval.count('value="approved"') == 2
        assert "이번 계획의 작업과 선택 이유" in body(approval)
        service.approve(rid, approver="fixture")
        progress = client.get(f"/runs/{rid}").text
        assert progress == client.get(f"/runs/{rid}/progress").text
        assert 'data-environment="local"' in progress and 'data-environment="cloud"' in progress
        assert "온프레미스" in body(progress)
        sentinel = "not-a-real" + "-private-value"
        raw = json.dumps({"password": sentinel, "code": "ADAPTER_FAILED"})
        service.store.finish(
            rid,
            "FAILED_CLOUD",
            {
                "tracks": {"local": "DONE", "cloud": "FAILED"},
                "steps": {
                    "deploy.was.cloud": {
                        "tool": "deploy_tier",
                        "status": "failed",
                        "elapsed_s": 3,
                        "error": raw,
                    }
                },
            },
            {},
            {},
        )
        result = client.get(f"/runs/{rid}/result").text
        assert sentinel not in result
        assert "ADAPTER_FAILED" not in body(result)
        assert "WAS 컨테이너 교체" in body(result)
        for html in (approval, progress, result):
            visible = body(html)
            assert not re.search(r'\{\s*"[^"]+"\s*:', visible)
            assert "온프렘" not in visible
            assert "[가림]" not in html


def luminance(hex_color):
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return sum(a * b for a, b in zip(channels, (0.2126, 0.7152, 0.0722), strict=True))


def contrast(a, b):
    lo, hi = sorted((luminance(a), luminance(b)))
    return (hi + 0.05) / (lo + 0.05)


def test_all_text_tokens_meet_aa_on_used_surfaces():
    root = Path(__file__).resolve().parents[3]
    css = (root / "src/ddak/web/static/app.css").read_text()
    tokens = dict(re.findall(r"--([a-z0-9-]+):\s*(#[a-f0-9]{6})", css))
    for foreground in ("ink", "ink-2", "ink-3"):
        for background in ("bg", "surface-1", "surface-2", "surface-code"):
            assert contrast(tokens[foreground], tokens[background]) >= 4.5
    for foreground, background in (
        ("success", "success-bg"),
        ("failure", "failure-bg"),
        ("waiting", "waiting-bg"),
        ("accent", "accent-bg"),
        ("diff-del-ink", "diff-del-bg"),
        ("diff-add-ink", "diff-add-bg"),
    ):
        assert contrast(tokens[foreground], tokens[background]) >= 4.5
    assert contrast(tokens["cta-ink"], tokens["accent"]) >= 4.5
    assert not {"#ffffff", "#000000"} & set(tokens.values())


def test_file_context_mask_keeps_keys_and_proxy_structure(tmp_path):
    import difflib

    from ddak.core.code_mask import code_changes, masked_code

    secret = "fixture-private" + "-proxy-value"
    old = (
        f'app.config["SECRET_KEY"] = "{secret}"\n'
        "app.wsgi_app = ProxyFix(\n    app.wsgi_app, x_for=1,\n)\n"
    )
    new = (
        'app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]\n'
        "app.wsgi_app = ProxyFix(\n    app.wsgi_app, x_for=2,\n)\n"
    )
    (tmp_path / "app.py").write_text(old)
    patch = "".join(
        difflib.unified_diff(
            old.splitlines(True), new.splitlines(True), "a/app.py", "b/app.py", n=1
        )
    )
    cards = code_changes(patch, tmp_path)
    safe = json.dumps(cards)
    assert secret not in safe and "SECRET_KEY" in safe and "ProxyFix(" in safe
    assert {row["kind"] for card in cards for row in card["rows"]} >= {"ctx", "del", "add"}
    for raw in ('KEY="unterminated', f'KEY="""{secret}\n{secret}"""\n'):
        masked = masked_code(raw)
        assert secret not in masked and len(masked.splitlines()) == len(raw.splitlines())


def test_progress_restores_one_finished_environment_without_infra_waiting(rig):
    service, source, _ = rig
    rid = prepare(service, source)
    service.approve(rid, approver="fixture")
    events = [
        {
            "type": "step.started",
            "step": "deploy.was.local",
            "target": "local",
            "ts": "2026-10-04T01:00:00Z",
        },
        {
            "type": "step.finished",
            "step": "deploy.was.local",
            "target": "local",
            "status": "succeeded",
            "elapsed_s": 4,
        },
        {
            "type": "step.started",
            "step": "verify.smoke.local",
            "target": "local",
            "ts": "2026-10-04T01:00:04Z",
        },
        {
            "type": "step.finished",
            "step": "verify.smoke.local",
            "target": "local",
            "status": "succeeded",
            "elapsed_s": 2,
        },
        {
            "type": "gate.opened",
            "detail": "local_verified",
            "target": "local",
            "ts": "2026-10-04T01:00:06Z",
        },
        {
            "type": "step.started",
            "step": "deploy.was.cloud",
            "target": "cloud",
            "ts": "2026-10-04T01:00:03Z",
        },
    ]
    directory = service.root / "runs" / rid
    (directory / "events.jsonl").write_text(
        "".join(
            json.dumps({"run_id": rid, "seq": i, **event}) + "\n" for i, event in enumerate(events)
        )
    )
    (directory / "context.json").write_text(
        json.dumps({"images": {"was": "repo@sha256:" + "a" * 64}})
    )
    before = service.get_run(rid)
    snapshot = service.track_result(rid)
    assert snapshot["run"]["result"]["tracks"]["local"] == "DONE"
    assert snapshot["run"]["result"]["track_elapsed_s"]["local"] == 6
    assert service.get_run(rid) == before
    with client_for(service) as client:
        html = client.get(f"/runs/{rid}").text
        assert html == client.get(f"/runs/{rid}").text
    local = html.split('data-track-result="local"', 1)[1].split("</section>", 1)[0]
    assert 'data-code="DONE"' in local and "a" * 12 in local and "사용자 시나리오" in local
    assert 'is-working" data-track-result="cloud"' in html
    assert 'id="final-review"' in html and 'class="final-review is-working"' in html
    assert (
        'data-step-row="deploy.was.cloud"' in html
        and 'is-running" data-step-row="deploy.was.cloud"' in html
    )
    assert 'data-step-row="deploy.infra.local"' not in html and "이벤트 대기" not in html
    assert 'id="total-clock"' in html and 'id="current-step-clock"' in html
    assert '<details class="story-technical"><summary>자세한 기록 보기' in html


def test_resource_tiles_and_storage_flow_keep_raw_identifiers_folded():
    from ddak.web.dependencies import templates
    from ddak.web.story import infra_story
    from tests.unit.test_approval_meta import summary

    infra = summary()
    infra.update(headline="클라우드 플랫폼 flaskr · ALB 80→443 · HCL source=cache")
    infra["iam_diff"][0]["address"] = "aws_iam_role_policy.codebuild"
    infra["iam_diff"][0]["added"][0]["resources"].append("[REDACTED]")
    template = templates.get_template("_resources.html")
    html = template.render(story={"infra": infra_story(infra)})
    visible = body(html)
    assert html.count("data-resource-count=") == 4
    assert "CodeBuild 역할" in visible and "시크릿 · 읽기" in visible
    assert not any(raw in visible for raw in ("arn:", "aws_", "[REDACTED]"))
    assert "resource-decision-slot" not in html
    storage = {
        "intent": "create",
        "rationale": [
            "로컬 이미지 디렉터리 탐지",
            "공용 저장소 필요",
            "저장소 코드 준비",
            "근거 확인",
            "승인 후 적용",
        ],
        "evidence": [{"file": "app.py", "line": 9, "kind": "local_directory"}],
        "bucket": "fixture-images",
        "env": {"IMG_DIR": "[REDACTED]"},
        "files": {"storage.tf": 'resource "aws_s3_bucket" "images" {}'},
        "source": "cache",
    }
    infra["storage"] = storage
    html = template.render(story={"infra": infra_story(infra)})
    assert "생성 4 · 삭제 0" in html and "app.py:9" in html
    positions = [
        html.index(f"<strong>{name}</strong>")
        for name in ("탐지", "필요 판단", "S3 생성 Terraform", "승인 요청", "자동 적용")
    ]
    assert positions == sorted(positions)
    assert "aws_s3_bucket" not in body(html)
    storage["intent"] = "remove"
    html = template.render(story={"infra": infra_story(infra)})
    assert "S3 이미지 저장소 삭제 · fixture-images (업로드 이미지 포함)" in html
    assert 'class="state failure">생성 0 · 삭제 4' in html


def test_result_banner_and_short_chips_have_no_raw_status_or_scenario(rig):
    service, source, _ = rig
    rid = prepare(service, source)
    service.store.finish(
        rid,
        "SUCCEEDED",
        {
            "tracks": {"local": "DONE", "cloud": "DONE"},
            "steps": {
                "verify.health.local": {"status": "succeeded", "elapsed_s": 2},
                "verify.smoke.local": {
                    "status": "succeeded",
                    "output": {"scenarios": [{"id": "S0.version", "ok": True}]},
                },
            },
        },
        {"images": {"was": "repo@sha256:" + "a" * 64}},
        {},
    )
    service.reports.get = lambda _: {
        "state": "ready",
        "source": "ai",
        "narrative": {
            "conclusion": "SUCCEEDED · verify.health.local · null " * 8
            + "마지막 항목까지 확인했습니다.",
            "changes": ["S0.version 확인", "verify.smoke.local 통과"],
            "checks": [],
            "next_action": "verify.diagnose",
        },
    }
    with client_for(service) as client:
        html = client.get(f"/runs/{rid}/result").text
    visible = body(html)
    assert (
        html.index("<h1>배포 완료</h1>")
        < html.index('id="result-metrics"')
        < html.index('id="report-summary"')
    )
    assert not any(raw in visible for raw in ("verify.", "S0.", "null", "SUCCEEDED"))
    chips = html.split('<ul class="summary-chips">', 1)[1].split("</ul>", 1)[0]
    lines = re.findall(r"<li>(.*?)</li>", chips)
    assert len(lines) <= 3 and len(lines[0]) > 56
    assert lines[0].endswith("마지막 항목까지 확인했습니다.")
    assert "…" not in chips
    assert "data-pipeline-rail" not in html
    assert 'id="failure"' not in html


def test_preparation_events_update_dashboard_checklist_and_live_version(rig):
    service, source, _ = rig
    rid = prepare(service, source)
    service._preparing_runs["flaskr-three"] = rid
    path = service.root / "runs" / rid / "events.jsonl"
    path.write_text(
        json.dumps(
            {
                "run_id": rid,
                "seq": 0,
                "type": "stage.finished",
                "preparation_stage": "intake",
                "status": "succeeded",
                "elapsed_s": 0.2,
            }
        )
        + "\n"
    )
    with client_for(service) as client:
        initial = client.get("/?project=flaskr-three").text
        with path.open("a") as file:
            file.write(
                json.dumps(
                    {
                        "run_id": rid,
                        "seq": 1,
                        "type": "stage.finished",
                        "preparation_stage": "detect",
                        "status": "succeeded",
                        "elapsed_s": 0.4,
                    }
                )
                + "\n"
            )
        updated = client.get("/?project=flaskr-three").text
    assert 'id="preparation-checklist"' in initial and wording("detect")["finished"] in body(
        updated
    )
    assert (
        re.search(r'data-live-version="([^"]+)"', initial)[1]
        != re.search(r'data-live-version="([^"]+)"', updated)[1]
    )
    assert 'id="connection-checklist"' not in updated
