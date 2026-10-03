"""표시 문장·가림 종류·대시보드 계획·레인 시작 시각 회귀."""

import copy
import json

import pytest

from ddak.core.code_mask import masked_code
from ddak.web.dependencies import templates
from ddak.web.narrative import explain, outcome_sentence, pipeline_view, wording
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_ui_integration_fix10 import client_for, prepare


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("입력 트리 변경 없음", "이전 이미지를 재사용"),
        ("인프라 입력 변경 없음", "클라우드 구성이 같아"),
        ("R-mandatory: 필수 step은 뺄 수 없다", "필수 작업을 유지"),
        ("온프렘 연결 정보를 확인하세요.", "온프레미스 연결 정보를 확인하세요."),
        ("new_warning: 이 변경은 새 배포에서 적용됩니다.", "이 변경은 새 배포에서 적용됩니다."),
    ],
)
def test_rule_explanations_preserve_distinct_human_meaning(raw, expected):
    assert expected in explain(raw, "rule")


def test_unknown_human_explanation_masks_secrets_and_raw_json():
    secret = "private-" + "explanation-fixture"
    rendered = explain(f"연결 설정 확인: password={secret}")
    assert secret not in rendered
    assert "[가림 · 비밀값]" in rendered
    raw = json.dumps({"password": secret, "code": "ADAPTER_FAILED", "detail": "응답 없음"})
    assert explain(raw) == "대상 환경에서 작업을 완료하지 못했습니다."
    assert "{" not in explain('{"detail": "연결 오류"}')


def test_json_keeps_korean_and_html_escaping():
    rendered = templates.env.from_string("{{ data|tojson }}").render(
        data={"이유": "클라우드 구성 변경", "tag": "</script>"}
    )
    assert "클라우드 구성 변경" in rendered
    assert "\\u클" not in rendered
    assert "</script>" not in rendered
    assert json.loads(rendered)["tag"] == "</script>"


def test_mask_names_kind_without_changing_environment_keys():
    secret = "private-" + "typed-fixture"
    address = "http://fixture.invalid/local"
    code = (
        f'SECRET_KEY = "{secret}"\n'
        f'DB_URL = "{address}"\n'
        "PORT = 4321\n"
        f"# {secret}\n"
        'SECRET_KEY = os.environ["SECRET_KEY"]\n'
    )
    masked = masked_code(code)
    assert secret not in masked and address not in masked and "4321" not in masked
    assert all(f"[가림 · {kind}]" in masked for kind in ("비밀값", "주소", "숫자", "주석"))
    assert 'os.environ["SECRET_KEY"]' in masked
    assert "[REDACTED]" not in masked


def test_unparseable_code_masks_entire_line():
    secret = "private-" + "incomplete"
    assert masked_code(f'x = "{secret}') == "[가림 · 코드 줄]"


def test_approval_actor_is_human():
    assert wording("request_approval")["actor"] == "사람 승인"


def test_lane_clock_uses_first_recorded_environment_start_not_current_step():
    events = [
        {
            "type": "step.started",
            "step": "deploy.was.local",
            "target": "local",
            "ts": "2026-10-04T10:00:30+00:00",
        },
        {
            "type": "step.started",
            "step": "deploy.storage.local",
            "target": "local",
            "ts": "2026-10-04T10:00:05+00:00",
        },
        {
            "type": "step.started",
            "step": "deploy.infra.cloud",
            "target": "cloud",
            "ts": "2026-10-04T10:00:10+00:00",
        },
    ]
    view = pipeline_view({"status": "RUNNING"}, {}, events)
    assert view["lanes_progress"]["local"]["started"] == events[1]["ts"]
    assert view["lanes_progress"]["cloud"]["started"] == events[2]["ts"]
    assert view["lanes_progress"]["common"]["started"] is None


@pytest.mark.parametrize(
    ("output", "count"),
    [
        ({}, None),
        ({"counts": {"create": 2, "update": 1, "delete": 0, "replace": 0}}, 3),
        ({"counts": {"create": 0, "update": 0, "delete": 0, "replace": 0}}, 0),
        ({"counts": {"create": "two"}}, None),
        ({"counts": {"create": True}}, None),
    ],
)
def test_infra_change_count_requires_recorded_numbers(output, count):
    row = {"id": "deploy.infra.cloud", "tool": "apply_infra", "finished": "인프라 적용 완료"}
    sentence = outcome_sentence(row, {"output": output})
    if count is None:
        assert "(변경 " not in sentence
    else:
        assert f"(변경 {count})" in sentence


def test_dashboard_has_no_decorative_rail_with_or_without_infra(rig, monkeypatch):
    service, source, _ = rig
    prepare(service, source)
    original = service.get_display_data

    def display(run_id):
        data = copy.deepcopy(original(run_id))
        data["plan"]["deploy"]["cloud"]["steps"].insert(
            0, {"id": "deploy.infra.cloud", "tool": "apply_infra", "target": "cloud"}
        )
        return data

    with client_for(service) as client:
        before = client.get("/?project=flaskr-three")
        assert before.status_code == 200
        assert "data-pipeline-rail" not in before.text
        monkeypatch.setattr(service, "get_display_data", display)
        after = client.get("/?project=flaskr-three")
        assert after.status_code == 200
        assert "data-pipeline-rail" not in after.text


def test_dashboard_failure_uses_error_dictionary(rig, monkeypatch):
    service, _, _ = rig
    secret = "private-" + "dashboard-fixture"
    monkeypatch.setattr(
        service,
        "list_preparations",
        lambda _: [
            {
                "status": "FAILED_BEFORE_DEPLOY",
                "detail": f"ADAPTER_FAILED: password={secret}",
                "created": 1,
                "request_id": "fixture",
                "run_id": None,
            }
        ],
    )
    with client_for(service) as client:
        response = client.get("/?project=flaskr-three")
        assert response.status_code == 200
        html = response.text
        assert "대상 환경에서 작업을 완료하지 못했습니다." in html
        assert secret not in html and "ADAPTER_FAILED:" not in html
