"""source=fixture: 저장된 S3 계획을 승인·진행·대시보드에서 렌더한다. 외부 실행 없음."""

import json
import re

import pytest

from ddak.web.dependencies import templates
from ddak.web.narrative import pipeline_view, planned_rows, wording
from ddak.web.story import approval_story
from tests.unit.test_approval_meta import HASH, summary
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_ui_integration_fix10 import PROJECT, client_for, prepare

BUCKET = "gerbera-flaskr-images-3"
REASON = "로컬 img 디렉토리 저장 코드 탐지 → 클라우드 공유 저장소 필요"
# storage_summary 계약: 판단 흐름은 정확히 5단계 문장이다.
RATIONALE = (
    "flaskr/app.py가 업로드 파일을 로컬 img 디렉토리에 저장",
    "클라우드는 컨테이너 교체 때 로컬 파일이 사라져 공유 저장소 필요",
    "S3 버킷과 공개 차단·암호화·앱 역할 권한을 Terraform으로 작성",
    "인프라 변경과 함께 사람 승인 요청",
    "승인 뒤 apply_infra가 적용하고 IMG_DIR을 주입",
)
RULE_FILL = "이미지 저장 경로를 필수 환경변수로 전환; 규칙으로 보완"
PLAN = {
    "deploy": {
        "cloud": {"steps": [{"id": "deploy.infra.cloud", "tool": "apply_infra"}]},
        "local": {"steps": [{"id": "deploy.infra.local", "tool": "apply_infra"}]},
    }
}


def storage_fixture(intent):
    return {
        "intent": intent,
        "rationale": list(RATIONALE),
        "evidence": [{"file": "flaskr/app.py", "line": 12, "kind": "local_directory"}],
        "bucket": BUCKET,
        "env": {"IMG_DIR": f"s3://{BUCKET}/img"},
        "files": {"storage.tf": 'resource "aws_s3_bucket" "images" {}'},
        "source": "fixture",
    }


def storage_view(service, rid, intent, monkeypatch):
    # 실제 storage_summary와 같은 모양의 fixture를 읽기 전용 승인 view에 연결한다.
    original = service.approval_view

    def fixture_view(run_id):
        view = original(run_id)
        if run_id == rid:
            view["infra_summary"]["storage"] = storage_fixture(intent)
        return view

    monkeypatch.setattr(service, "approval_view", fixture_view)


def name_for(intent):
    return (
        f"S3 이미지 저장소 삭제 · {BUCKET} (업로드 이미지 포함)"
        if intent == "remove"
        else f"S3 이미지 저장소 신규 생성 · {BUCKET}"
    )


@pytest.mark.parametrize("intent", ["create", "remove"])
def test_storage_approval_plan_and_resources_render(rig, intent):
    service, source, _ = rig
    rid = prepare(service, source, subjects={"infra": HASH}, infra_summary=summary())
    view = service.approval_view(rid)
    view["plan"] = PLAN
    view["infra_summary"]["storage"] = storage_fixture(intent)
    story = approval_story(view)
    row = next(row for row in story["plan_rows"] if row["id"] == "deploy.infra.cloud")
    assert row["name"] == name_for(intent)
    assert row["warning"] is (intent == "remove")
    assert story["plan_rows"][1]["name"] == wording("deploy.infra.local")["name"]
    html = templates.get_template("approval.html").render(
        approval=view,
        story=story,
        project=PROJECT,
        csrf_token="fixture",
        request={"url": {"path": "/approval"}},
    )
    plan_html = html.split('id="decision-basis"', 1)[1].split("</section>", 1)[0]
    resource_html = html.split('id="resource-decision-slot"', 1)[1].split("</section>", 1)[0]
    assert name_for(intent) in plan_html and name_for(intent) in resource_html
    assert "검증용 데이터" in resource_html and "flaskr/app.py:12" in resource_html
    titles = ["탐지", "필요 판단", "승인 요청", "자동 적용"]
    assert all(f"<strong>{title}</strong>" in resource_html for title in titles)
    assert all(text in resource_html for text in RATIONALE)
    assert "판단 근거 기록 없음" not in resource_html
    assert BUCKET in resource_html and f"s3://{BUCKET}/img" in resource_html
    if intent == "create":
        assert "<strong>S3 생성 Terraform</strong>" in resource_html
        assert "생성 4 · 삭제 0" in resource_html
    else:
        assert "<strong>이전 Terraform 복구</strong>" in resource_html
        assert "생성 0 · 삭제 4" in resource_html
    if intent == "create":
        assert REASON in plan_html
    else:
        assert f'class="state failure">{name_for(intent)}' in plan_html
        assert f'class="state failure">{name_for(intent)}' in resource_html


@pytest.mark.parametrize("intent", ["create", "remove", None])
def test_dashboard_checklist_reads_saved_storage(rig, intent, monkeypatch):
    service, source, _ = rig
    rid = prepare(service, source, subjects={"infra": HASH}, infra_summary=summary())
    if intent:
        storage_view(service, rid, intent, monkeypatch)
    with client_for(service) as client:
        response = client.get(f"/?project={PROJECT}")
    assert response.status_code == 200
    if intent is None:
        assert "S3 저장소 생성 계획" not in response.text
        assert 'id="preparation-checklist"' not in response.text
    else:
        checklist = response.text.split('id="preparation-checklist"', 1)[1].split("</section>", 1)[
            0
        ]
        if intent == "create":
            assert "S3 저장소 생성 계획" in checklist and REASON in checklist
        else:
            assert 'class="state failure">S3 저장소 삭제 계획 (업로드 이미지 포함)' in checklist


@pytest.mark.parametrize("intent", ["create", "remove", None])
def test_progress_ssr_current_row_and_sse_dictionary_use_saved_storage(rig, intent, monkeypatch):
    service, source, calls = rig
    rid = prepare(service, source, subjects={"infra": HASH}, infra_summary=summary())
    service.approve(rid, approver="fixture")
    if intent:
        storage_view(service, rid, intent, monkeypatch)
    directory = service.root / "runs" / rid
    events = [
        {
            "run_id": rid,
            "seq": index,
            "type": "step.started",
            "step": sid,
            "target": target,
            "ts": "2026-10-04T01:00:00Z",
        }
        for index, (sid, target) in enumerate(
            [("deploy.was.local", "local"), ("deploy.infra.cloud", "cloud")]
        )
    ]
    (directory / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
    before = service.get_run(rid)
    with client_for(service) as client:
        response = client.get(f"/runs/{rid}/progress")
    assert response.status_code == 200
    assert service.get_run(rid) == before and calls.contexts == []
    html = response.text
    current = re.search(r'<p id="current-activity">(.*?)</p>', html)[1]
    lane = html.split('data-step-row="deploy.infra.cloud"', 1)[1].split("</li>", 1)[0]
    dictionary = json.loads(
        re.search(r'id="pipeline-wording" data-narrative>(.*?)</script>', html)[1]
    )
    text = dictionary["texts"]["deploy.infra.cloud"]
    assert dictionary["aliases"]["deploy.infra"] == "apply_infra"
    assert "온프레미스" in current and "클라우드" in current
    if intent is None:
        assert text == wording("deploy.infra.cloud")
        assert text["running"] in current and text["running"] in lane
        assert "S3 버킷" not in html
    else:
        action = "삭제" if intent == "remove" else "생성"
        running = f"S3 버킷 {action} 중 · {BUCKET}"
        assert running in current and running in lane and running in text["running"]
        assert text["name"] == name_for(intent)
        if intent == "remove":
            assert f'class="state failure">{name_for(intent)}' in lane
        else:
            assert text["description"] == REASON


def test_no_storage_keeps_existing_plan_and_pipeline_wording():
    rows = planned_rows(PLAN)
    assert rows == planned_rows(PLAN, storage=None)
    assert rows[0]["name"] == wording("deploy.infra.cloud")["name"]
    view = pipeline_view({"status": "RUNNING"}, PLAN, [])
    assert view["texts"]["deploy.infra.cloud"] == wording("deploy.infra.cloud")
    assert "S3" not in rows[0]["name"]


@pytest.mark.parametrize("reason", [RULE_FILL, "AI 제안 그대로 적용"])
def test_approval_shows_rule_filled_img_dir_patch(rig, reason):
    service, source, _ = rig
    rid = prepare(service, source, subjects={"infra": HASH}, infra_summary=summary())
    view = service.approval_view(rid)
    view["patch_meta"] = {"reason": reason, "passed": True, "source": "replay", "reuse": False}
    html = templates.get_template("approval.html").render(
        approval=view,
        story=approval_story(view),
        project=PROJECT,
        csrf_token="fixture",
        request={"url": {"path": "/approval"}},
    )
    changes = html.split('id="code-changes"', 1)[1].split("</section>", 1)[0]
    if reason == RULE_FILL:
        assert f'<p data-patch-rule-fill><span class="actor">규칙 보완</span> {RULE_FILL}</p>' in (
            changes
        )
    else:
        assert "data-patch-rule-fill" not in changes and reason not in changes
