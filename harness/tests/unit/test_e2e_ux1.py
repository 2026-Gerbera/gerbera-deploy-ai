"""승인 동작 위치·접힌 판정 원문·빈 오류 박스 회귀. 외부 실행 없음."""

import pytest

from ddak.web.dependencies import templates
from tests.unit.test_ui_integration_fix10 import PROJECT, client_for, prepare
from tests.unit.test_ui_integration_fix10 import rig as rig


def test_approval_actions_precede_details_and_basis_is_summarized(rig):
    service, source, _ = rig
    rid = prepare(service, source)
    view = service.approval_view(rid)
    view["decision_basis"] = {
        "planner": {"provider": "claude-cli", "model": "fixture-model"},
        "steps": [{"id": "one", "reason": "long-reason-" * 1000}, {"id": "two"}],
        "skipped": [{"id": "three"}],
        "env_classification": {
            "facts_available": True,
            "by_provider": {
                "rule": [{"kind": "secret"}],
                "claude-cli": [{"kind": "plain"}, {"kind": "plain"}],
            },
        },
    }
    html = templates.get_template("approval.html").render(
        approval=view,
        project=view["project"],
        csrf_token="fixture",
        request={"url": {"path": "/approval"}},
    )
    before, technical = html.split('<details id="approval-technical">', 1)
    assert "<summary>기술 정보</summary>" in technical
    assert "long-reason-" not in before and "long-reason-" in technical
    # 한 폼 안에 상단(요약 바로 아래)과 본문 끝 결정 바가 있다. 같은 POST·같은 승인 해시를 쓴다.
    assert html.count("data-approval-form") == 1
    assert html.count('value="approved"') == html.count('value="denied"') == 2
    assert before.index('class="approval-bar"') < before.index("준비 경고")
    assert before.index('class="approval-bar"') < before.index('id="decision-basis"')
    assert "필수 단계·허용 입력·DB 변경 순서" in before
    assert "배포 계획" in before
    assert "총 3개 · 비밀 1개 · 일반 2개" in before
    assert "fixture-model" not in before and "fixture-model" in technical


@pytest.mark.parametrize("page", ["approval", "result", "ops"])
def test_no_error_boxes_outside_inert_template(rig, page):
    service, source, _ = rig
    rid = prepare(service, source)
    if page == "result":
        service.approve(rid, approver="fixture", approved=False)
    path = f"/ops?project={PROJECT}" if page == "ops" else f"/runs/{rid}/{page}"
    with client_for(service) as client:
        response = client.get(path)
        assert response.status_code == 200
        visible, prototype = response.text.split('<template id="form-error-template">', 1)
        assert "data-form-error" not in visible and "⚠" not in visible
        assert "data-error-message" in prototype and "data-error-code" in prototype
        assert "hidden" in prototype.split("</template>", 1)[0]
        if page == "approval":
            assert client.get(f"/ops/runs/{rid}/approval").text == response.text


def test_error_macro_only_renders_real_errors_or_inert_prototype():
    macro = templates.get_template("_status.html").module.error_box
    assert not macro().strip()
    error = macro({"code": "CONFIG_INVALID", "message": "설정 확인 필요"})
    assert 'role="alert"' in error and "hidden" not in error.split(">", 1)[0]
    assert "✕" in error and "오류" in error and "설정 확인 필요" in error
