"""source=fixture: 결과 화면의 집계·다음 행동·환경별 기록만 검증한다."""

import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ddak.web.dependencies import templates
from ddak.web.routes.results import router
from ddak.web.story import environment_cards, result_story, step_title


@pytest.fixture
def recorded():
    old_image = "fixture@sha256:" + "a" * 64
    new_image = "fixture@sha256:" + "b" * 64
    run = {
        "run_id": "result-fixture",
        "project": "fixture",
        "status": "SUCCEEDED",
        "created": 10,
        "finished": 70,
        "context": {
            "targets": "both",
            "ref": "v2",
            "source_sha": "b" * 40,
            "public_url": "https://local.example.com",
            "cloud_domain": "cloud.example.com",
        },
        "result": {
            "tracks": {"local": "DONE", "cloud": "DONE"},
            "steps": {
                "build.was": {"status": "succeeded", "tool": "build_image"},
                "build.web": {"status": "skipped", "tool": "build_image"},
                "verify.health.local": {"status": "succeeded"},
                "verify.smoke.local": {
                    "status": "succeeded",
                    "output": {"scenarios": [{"id": "B1", "ok": True}, {"id": "B2", "ok": True}]},
                },
                "verify.smoke.cloud": {
                    "status": "succeeded",
                    "output": {"scenarios": [{"id": "B1", "ok": True}]},
                },
                "verify.diagnose": {"status": "skipped"},
            },
        },
    }
    release = {"images": {"was": new_image, "web": old_image}, "source_sha": "b" * 40}
    data = {
        "previous": {
            env: {
                "ref": "v1",
                "source_sha": "a" * 40,
                "images": {"was": old_image, "web": old_image},
            }
            for env in ("local", "cloud")
        },
        "plan": {
            "build": {
                "steps": [{"id": "build.was", "tool": "build_image", "tier": "was"}],
                "skipped": [
                    {
                        "id": "build.web",
                        "tool": "build_image",
                        "tier": "web",
                        "skip_rule": "tree_unchanged",
                    }
                ],
            },
            "deploy": {
                env: {
                    "steps": [
                        {"id": f"deploy.{tier}.{env}", "tool": "deploy_tier", "tier": tier}
                        for tier in ("was", "web")
                    ]
                }
                for env in ("local", "cloud")
            },
        },
    }
    return run, release, data


def render_result(recorded):
    run, release, data = recorded
    service = SimpleNamespace(
        get_run=lambda _: run,
        get_release=lambda _: release,
        get_display_data=lambda _: data,
        events=lambda _: [],
        reports=SimpleNamespace(get=lambda _: None),
    )
    app = FastAPI()
    app.state.deployment = service
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get(f"/runs/{run['run_id']}/result")
    assert response.status_code == 200
    return response.text


def section(html, marker):
    return html.split(marker, 1)[1].split("</section>", 1)[0]


def test_success_skipped_diagnosis_is_neutral(recorded):
    html = render_result(recorded)
    diagnosis = section(html, 'id="failure-diagnosis"')
    assert "— 원인 분석: 실패가 없어 생략" in diagnosis
    assert "추정" not in diagnosis and "failure" not in diagnosis
    story = result_story(*recorded)
    assert story["diagnose"]["hypothesis"] is False


def test_scenarios_count_only_scenarios_and_checks_appear_once(recorded):
    story = result_story(*recorded)
    assert story["metrics"]["passed"] == story["metrics"]["checks"] == 3
    html = render_result(recorded)
    card = section(html, 'data-track-result="local"')
    assert card.count(step_title("verify.health.local")) == 1
    assert card.count(step_title("verify.smoke.local")) == 1
    assert re.search(r"사용자 시나리오\s*2\s*/\s*2", card)
    assert 'id="verification-results"' not in html


@pytest.mark.parametrize(
    ("status", "phase", "expected"),
    [
        ("SUCCEEDED", None, None),
        ("FAILED_CLOUD", None, "/ops?project=fixture"),
        ("NEEDS_HUMAN", None, "/ops?project=fixture"),
        ("FAILED_BEFORE_DEPLOY", "settings", "/settings?project=fixture"),
        ("SUPERSEDED", None, "/runs/next-fixture/approval"),
    ],
)
def test_result_has_at_most_one_followup(recorded, status, phase, expected):
    run, _, _ = recorded
    run["status"] = status
    run["result"].update(phase=phase, superseded_by="next-fixture")
    if status in {"FAILED_CLOUD", "NEEDS_HUMAN", "FAILED_BEFORE_DEPLOY"}:
        run["result"]["steps"]["deploy.was.cloud"] = {"status": "failed"}
        run["result"]["tracks"]["cloud"] = "FAILED"
    html = render_result(recorded)
    main = html.split('<main id="main">', 1)[1].split("</main>", 1)[0]
    actions = re.findall(r'<a class="button[^\"]*" href="([^\"]+)"', main)
    followups = [url for url in actions if not url.startswith("https://")]
    assert followups == ([expected] if expected else [])
    if status == "SUCCEEDED":
        assert actions.count("https://local.example.com") == 1
        assert actions.count("https://cloud.example.com") == 1
    if run["result"]["tracks"]["cloud"] == "FAILED":
        assert "https://cloud.example.com" not in actions


def test_recorded_versions_commits_and_shared_builds_reach_both_cards(recorded):
    html = render_result(recorded)
    for env in ("local", "cloud"):
        card = section(html, f'data-track-result="{env}"')
        assert "v1" in card and "v2" in card
        assert "aaaaaaa" in card and "bbbbbbb" in card
        assert "WAS 이미지 빌드" in card and "새로 빌드" in card
        assert "WEB 이미지 빌드" in card and "재사용" in card


def test_missing_version_record_is_not_inferred_from_commits(recorded):
    run, _, data = recorded
    run["context"]["ref"] = "prod"
    for previous in data["previous"].values():
        previous.pop("ref")
    html = render_result(recorded)
    card = section(html, 'data-track-result="local"')
    assert "버전 기록 없음" in card
    assert "v1" not in card and "v2" not in card


@pytest.mark.parametrize(
    "status", ["RUNNING", "WAITING", "PENDING", "FAILED", "ROLLED_BACK", "N/A"]
)
def test_incomplete_environment_never_links_service(recorded, status):
    run, _, _ = recorded
    run["status"] = "RUNNING"
    run["result"]["tracks"]["cloud"] = status
    links = [{"label": "클라우드", "url": "https://cloud.example.com"}]
    cards = environment_cards(run, result_story(*recorded), links)
    assert cards[1]["link"] is None
    html = templates.get_template("_environment_cards.html").render(environment_cards=cards)
    assert "서비스 열기" not in html


def test_failed_scenario_is_counted_without_counting_health_steps(recorded):
    run, _, _ = recorded
    run["status"] = "FAILED_VERIFY"
    record = run["result"]["steps"]["verify.smoke.local"]
    record["status"] = "check_failed"
    record["output"]["scenarios"][1]["ok"] = False
    story = result_story(*recorded)
    assert story["metrics"]["passed"] == 2
    assert story["metrics"]["checks"] == 3
    html = render_result(recorded)
    card = section(html, 'data-track-result="local"')
    assert re.search(r"사용자 시나리오\s*1\s*/\s*2", card)
    assert "✕" in card
    diagnosis = section(html, 'id="failure-diagnosis"')
    assert "실패가 없어" not in diagnosis and "추정" not in diagnosis
