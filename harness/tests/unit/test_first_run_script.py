"""#32 병합 뒤 setup 전용 submit 처리의 중복 재등록을 막는다."""

from tests.unit.test_first_run import first_run as first_run
from tests.unit.test_first_run_web import Forms
from tests.unit.test_first_run_web import panel as panel


def test_setup_uses_one_common_submit_path(panel):
    client, _, _ = panel
    response = client.get("/setup?project=demo")
    assert response.status_code == 200
    assert response.text.count('src="/static/app.js') == 1
    assert "data-setup-form" not in response.text
    assert "initializeSetup" not in response.text
    for form in Forms(response.text).forms:
        assert form["fields"].get("_form_id")
        assert form["fields"].get("_return_to") == "/setup?project=demo"
    assert "data-form-error" in response.text
