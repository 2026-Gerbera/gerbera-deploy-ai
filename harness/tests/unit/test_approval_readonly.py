"""승인된 계획의 조회와 기존 승인 대기 HTML·승인 관문 회귀."""

import hashlib
from datetime import UTC, datetime

import pytest

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.executor.engine import RunStatus
from ddak.web.dependencies import templates
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_ui_integration_fix10 import BASE, client_for, prepare, summary


@pytest.mark.parametrize("enabled", [False, True])
def test_awaiting_korean_html_is_byte_identical(enabled):
    html = templates.get_template("approval.html").render(
        request={"url": {"path": "/runs/fixed/approval"}},
        approval={
            "run_id": "fixed",
            "project": "demo",
            "targets": "both",
            "source_sha": "a" * 40,
            "subjects": {},
        },
        project="demo",
        csrf_token="fixed-csrf",
        patch_review_enabled=enabled,
        patch_review=None,
        start_retry=False,
        read_only=False,
    )
    # 조회 전용 분기 추가 직전 한국어 렌더 바이트의 SHA-256.
    expected = (
        "5b97e7463e88e558b2de512aa1b43fa6a95da1e590a1acec700497ccca8755bd"
        if enabled
        else "14a5f1098b72ad22ca8a04960e3e1ec5a4dc1d8d68760f9b316c6bcfce596b89"
    )
    assert hashlib.sha256(html.encode()).hexdigest() == expected


@pytest.mark.parametrize("status", ["RUNNING", *RunStatus])
@pytest.mark.parametrize("lang", ["ko", "ja"])
def test_approved_plan_can_be_read_in_all_execution_states(rig, monkeypatch, status, lang):
    service, source, _ = rig
    rid = prepare(
        service, source, subjects={"infra": "sha256:" + "a" * 64}, infra_summary=summary()
    )
    records = service.approve(rid, approver="fixture")
    with service.store.connection() as db:
        db.execute("UPDATE runs SET status=? WHERE run_id=?", (status, rid))
    instant = datetime(2026, 10, 3, 16, 2, tzinfo=UTC)
    monkeypatch.setattr(
        service,
        "get_approvals",
        lambda _: [record.model_copy(update={"approved_at": instant}) for record in records],
    )
    original = service.get_display_data
    monkeypatch.setattr(
        service,
        "get_display_data",
        lambda run_id: {
            **original(run_id),
            "patches": [
                {
                    "file": "app.py",
                    "line": 4,
                    "end": 4,
                    "rows": [
                        {
                            "kind": "add",
                            "old": None,
                            "new": 4,
                            "text": 'value = os.environ["SECRET_KEY"]',
                        }
                    ],
                }
            ],
        },
    )
    with client_for(service) as client:
        response = client.get(f"/runs/{rid}/approval?lang={lang}", follow_redirects=False)
        assert response.status_code == 200
        html = response.text
        assert f'<html lang="{lang}">' in html
        assert "data-approval-readonly" in html
        assert "承認済み" in html if lang == "ja" else "승인됨" in html
        assert "01:02 (KST)" in html
        assert "<form" not in html and 'type="submit"' not in html
        assert 'value="approved"' not in html and 'value="denied"' not in html
        assert f'href="/runs/{rid}/patch-review"' not in html
        assert "data-live-region" not in html and "data-run-id" not in html
        assert 'class="code-change"' in html and "os.environ" in html
        assert 'id="resource-changes"' in html and 'id="decision-basis"' in html
        assert 'id="approval-subjects"' in html and 'id="approval-technical"' in html
        page = "progress" if status == "RUNNING" else "result"
        assert f'href="/runs/{rid}/{page}"' in html
        assert client.get(f"/ops/runs/{rid}/approval?lang={lang}").text == html


@pytest.mark.parametrize("status", ["RUNNING", "SUCCEEDED", "FAILED_CLOUD", "NEEDS_HUMAN"])
@pytest.mark.parametrize("decision", ["approved", "denied"])
def test_second_approval_is_rejected_without_changing_record(rig, status, decision):
    service, source, _ = rig
    rid = prepare(service, source)
    records = service.approve(rid, approver="fixture")
    with service.store.connection() as db:
        db.execute("UPDATE runs SET status=? WHERE run_id=?", (status, rid))
    with client_for(service) as client:
        assert client.get(f"/runs/{rid}/approval").status_code == 200
        response = client.post(
            f"/runs/{rid}/approval",
            data={"csrf_token": client.cookies.get("ddak_csrf"), "decision": decision},
            headers={"origin": BASE, "accept": "application/json", "x-ddak-form": "1"},
            follow_redirects=False,
        )
        assert response.status_code == 409
        assert service.get_approvals(rid) == records
        assert service.get_run(rid)["status"] == status


def test_awaiting_keeps_approval_form(rig):
    service, source, _ = rig
    rid = prepare(service, source)
    with client_for(service) as client:
        response = client.get(f"/runs/{rid}/approval")
        assert response.status_code == 200
        assert response.text.count("data-approval-form") == 1
        assert response.text.count('value="approved"') == 2
        assert "data-approval-readonly" not in response.text


def test_finished_plan_uses_saved_approval_view(rig):
    service, source, _ = rig
    rid = prepare(service, source)
    service.approve(rid, approver="fixture")
    service.store.finish(rid, "SUCCEEDED", {"steps": {}, "tracks": {}}, {}, {})
    with service.store.connection() as db:
        db.execute("DELETE FROM prepared_runs WHERE run_id=?", (rid,))
    with client_for(service) as client:
        response = client.get(f"/runs/{rid}/approval")
        assert response.status_code == 200
        assert "data-approval-readonly" in response.text
        assert "aaaaaaa" in response.text and "배포 계획" in response.text


def test_only_preparation_precondition_redirects_to_result(rig, monkeypatch):
    service, source, _ = rig
    rid = prepare(service, source)
    with client_for(service) as client:
        for code, expected in ((ErrorCode.PRECONDITION_FAILED, 303), (ErrorCode.INTERNAL, 409)):

            def fail(_, error_code=code):
                raise DdakToolError(error_code, "fixture")

            monkeypatch.setattr(service, "approval_view", fail)
            response = client.get(
                f"/runs/{rid}/approval", headers={"accept": "text/html"}, follow_redirects=False
            )
            assert response.status_code == expected
            if expected == 303:
                assert response.headers["location"] == f"/runs/{rid}/result"
            else:
                assert "location" not in response.headers


def test_without_approval_record_does_not_invent_timestamp(rig):
    service, source, _ = rig
    rid = prepare(service, source)
    with service.store.connection() as db:
        db.execute("UPDATE runs SET status='CANCELLED' WHERE run_id=?", (rid,))
    with client_for(service) as client:
        response = client.get(f"/runs/{rid}/approval")
        assert response.status_code == 200
        assert "조회 전용 · 승인 기록 없음" in response.text
        assert "(KST)" not in response.text and "<form" not in response.text
