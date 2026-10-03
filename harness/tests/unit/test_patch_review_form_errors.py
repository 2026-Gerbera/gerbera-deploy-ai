"""패치 검토에 관리 폼 오류 계약을 적용한 HTTP 회귀. 외부 도구는 호출하지 않는다."""

from html.parser import HTMLParser

import pytest

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from tests.unit.test_form_errors import Boxes, submit
from tests.unit.test_ui_integration_fix10 import client_for, post
from tests.unit.test_ui_patch_review import begin, wait_review
from tests.unit.test_ui_patch_review import review_rig as review_rig
from tests.unit.test_ui_patch_review import rig as rig

PATH = "/runs/review-parent/patch-review"


class Inputs(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.fields = {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            attrs = dict(attrs)
            self.fields[attrs.get("name")] = attrs


@pytest.mark.parametrize("js", [False, True])
@pytest.mark.parametrize(
    "action", ["begin", "save", "revise:cookie", "adopt", "keep", "finalize", "cancel"]
)
def test_each_patch_action_uses_common_error_response(review_rig, monkeypatch, action, js):
    service, _, calls, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        called = []

        def fail(*args, **kwargs):
            called.append(args)
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "검토 입력 <확인> 필요")

        monkeypatch.setattr(reviews, "begin" if action == "begin" else "request", fail)
        response = submit(
            client,
            PATH,
            PATH,
            "patch-review",
            js=js,
            action=action,
            revision=draft["revision"],
            apply_cookie="on",
            prompt_cookie="쿠키 설정을 다시 봐줘",
        )
        assert len(called) == 1 and not calls.contexts
        if js:
            assert response.status_code == 409
            assert response.json() == {
                "error": {
                    "code": "PRECONDITION_FAILED",
                    "message": "검토 입력 <확인> 필요",
                }
            }
            assert "location" not in response.headers
        else:
            assert response.status_code == 303
            assert response.headers["location"].startswith(PATH + "?_form_error=")
            response = client.get(response.headers["location"])
            assert Boxes(response.text).visible == ["patch-review"]
            assert "검토 입력 &lt;확인&gt; 필요" in response.text
            fields = Inputs(response.text).fields
            assert "checked" in fields["apply_cookie"]
            assert "checked" not in fields["apply_secret"]
            assert "쿠키 설정을 다시 봐줘</textarea>" in response.text
        assert reviews.get("review-parent") == draft
        assert response.headers["cache-control"] == "no-store"


def test_begin_error_uses_begin_form_and_invalid_revision_has_code(review_rig, monkeypatch):
    service, _, _, reviews, _ = review_rig
    with client_for(service) as client:
        client.get(PATH)

        def fail(_):
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "연결을 확인하세요")

        with monkeypatch.context() as m:
            m.setattr(reviews, "begin", fail)
            response = submit(client, PATH, PATH, "patch-begin", action="begin")
            assert response.status_code == 303
            assert Boxes(client.get(response.headers["location"]).text).visible == ["patch-begin"]
        begin(client, reviews)
        response = submit(
            client, PATH, PATH, "patch-review", js=True, action="save", revision="wrong"
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONFIG_INVALID"


def test_stale_inputs_cannot_overwrite_current_revision(review_rig):
    service, _, _, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        post(
            client,
            PATH,
            action="save",
            revision=draft["revision"],
            apply_secret="on",
            prompt_secret="저장된 새 메모",
        )
        current = reviews.get("review-parent")
        response = submit(
            client,
            PATH,
            PATH,
            "patch-review",
            action="save",
            revision=draft["revision"],
            apply_cookie="on",
            prompt_secret="오래된 입력",
        )
        page = client.get(response.headers["location"])
        fields = Inputs(page.text).fields
        assert "checked" in fields["apply_secret"] and "checked" not in fields["apply_cookie"]
        assert "저장된 새 메모</textarea>" in page.text
        assert "오래된 입력</textarea>" not in page.text
        assert "오래된 입력</pre>" in page.text and "data-stale-input" in page.text
        assert reviews.get("review-parent") == current


def test_csrf_rejection_does_not_cache_inputs_or_mutate(review_rig):
    service, _, _, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        response = submit(
            client,
            PATH,
            PATH,
            "patch-review",
            action="save",
            csrf_token="wrong",
            revision=draft["revision"],
            prompt_cookie="untrusted note",
        )
        assert response.status_code == 303
        assert "untrusted note" not in repr(client.app.state.form_flashes)
        assert reviews.get("review-parent") == draft
        assert "HTTP_403" in client.get(response.headers["location"]).text


@pytest.mark.parametrize("action", ["begin", "revise:cookie", "finalize"])
def test_async_failure_has_code_below_form_and_preserves_inputs(review_rig, monkeypatch, action):
    service, _, calls, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        if action == "begin":
            post(client, PATH, action="cancel", revision=draft["revision"])

            def fail(*args, **kwargs):
                raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "생성 연결 실패")

            monkeypatch.setattr(reviews, "generate", fail)
        elif action == "finalize":

            async def fail(*args):
                raise DdakToolError(ErrorCode.PLAN_INVALID, "조합 계획 검사 실패")

            monkeypatch.setattr(reviews, "finalize", fail)
        response = post(
            client,
            PATH,
            action=action,
            revision=draft["revision"],
            apply_cookie="on",
            prompt_cookie="실패",
        )
        assert response.status_code == 303
        wait_review(client, reviews)
        current = reviews.get("review-parent")
        assert current["error_code"] == (
            "PLAN_INVALID" if action == "finalize" else "AI_UNAVAILABLE"
        )
        page = client.get(PATH).text
        assert Boxes(page).visible == ["patch-review"]
        assert current["error_code"] in page and current["error"] in page
        if action != "begin":
            assert current["proposals"] == draft["proposals"]
            assert current["selected"] == ["cookie"]
            assert "실패</textarea>" in page
        assert not calls.contexts and service.get_approvals("review-parent") == []


def test_error_survives_parent_to_successor_approval_redirect(review_rig):
    service, _, _, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        post(client, PATH, action="finalize", revision=draft["revision"], apply_cookie="on")
        wait_review(client, reviews)
        response = submit(
            client,
            PATH,
            PATH,
            "patch-review",
            action="save",
            revision=draft["revision"],
            prompt_cookie="닫힌 검토의 초안",
            apply_cookie="on",
        )
        assert response.status_code == 303
        page = client.get(response.headers["location"])
        assert page.url.path == "/runs/review-child/approval"
        assert "PRECONDITION_FAILED" in page.text
        assert "닫힌 검토의 초안</pre>" in page.text
        assert len(Boxes(page.text).visible) == 1
        assert not client.app.state.form_flashes


def test_saved_controls_and_approval_alias_have_same_html(review_rig):
    service, _, _, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        page = client.get(PATH).text
        assert 'value="save"' in page and 'value="patch-review"' in page
        post(
            client,
            PATH,
            action="save",
            revision=draft["revision"],
            apply_cookie="on",
            prompt_cookie="다음 검토",
        )
        saved = client.get(PATH).text
        assert "다음 검토</textarea>" in saved
        assert (
            client.get("/runs/review-parent/approval").text
            == client.get("/ops/runs/review-parent/approval").text
        )


@pytest.mark.parametrize("js", [False, True])
def test_start_failure_retry_reuses_final_approval(review_rig, monkeypatch, js):
    service, _, calls, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        post(client, PATH, action="finalize", revision=draft["revision"], apply_cookie="on")
        wait_review(client, reviews)
        approval = "/runs/review-child/approval"
        client.get(approval)
        original = service.start

        def fail(_):
            raise DdakToolError(ErrorCode.LOCK_HELD, "잠금을 확인하세요")

        with monkeypatch.context() as m:
            m.setattr(service, "start", fail)
            response = submit(client, approval, approval, "approval", js=js, decision="approved")
            if js:
                assert response.status_code == 409
                assert response.json()["error"]["code"] == "LOCK_HELD"
            else:
                assert response.status_code == 303
                page = client.get(response.headers["location"])
                assert page.url.path == approval and page.status_code == 200
                assert Boxes(page.text).visible == ["approval"]
                assert "LOCK_HELD" in page.text and "배포 시작 재시도" in page.text
                assert "승인하고 배포</button>" not in page.text
                assert 'value="approved"' in page.text
                assert client.get(approval).text == client.get("/ops" + approval).text
        records = service.get_approvals("review-child")
        assert records and len({r.approval_id for r in records}) == 1
        assert not calls.contexts
        response = submit(client, approval, approval, "approval", js=js, decision="approved")
        assert response.status_code == 303
        client.portal.call(service.wait, "review-child")
        assert service.get_run("review-child")["status"] == "SUCCEEDED"
        assert service.get_approvals("review-child") == records
        with pytest.raises(DdakToolError, match="한 번만"):
            original("review-child")


def test_restarted_review_reports_interruption_in_form(review_rig):
    service, _, _, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        service.store.save_patch_review(
            "review-parent",
            draft["revision"],
            {
                **draft,
                "state": "reviewing",
                "error": None,
                "error_code": "AI_UNAVAILABLE",
                "notes": {"cookie": "저장된 요청"},
            },
        )
        service.store.recover_patch_reviews()
        page = client.get(PATH).text
        assert Boxes(page).visible == ["patch-review"]
        assert "서비스가 재시작돼" in page and "INTERNAL" in page
        assert "저장된 요청</textarea>" in page
        assert reviews.get("review-parent")["proposals"] == draft["proposals"]


def test_busy_review_keeps_failure_draft_without_timed_reload(review_rig):
    service, _, _, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        service.store.save_patch_review(
            "review-parent",
            draft["revision"],
            {
                **draft,
                "state": "reviewing",
            },
        )
        response = submit(
            client,
            PATH,
            PATH,
            "patch-review",
            action="save",
            revision=draft["revision"],
            prompt_cookie="작업 중에 보낸 초안",
        )
        page = client.get(response.headers["location"])
        assert "작업 중에 보낸 초안</pre>" in page.text
        assert 'http-equiv="refresh"' not in page.text
        assert "입력 복사 후 최신 상태 확인" in page.text
        assert Boxes(page.text).visible == [None]  # busy 상태에는 제출 폼이 없다.


def test_regeneration_preserves_draft_for_previous_proposal_ids(review_rig, monkeypatch):
    service, _, _, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        post(client, PATH, action="cancel", revision=draft["revision"])
        # 새 생성 작업이 아직 반환하지 않은 상태에서 이전 탭을 제출한다.
        monkeypatch.setattr(reviews, "_launch", lambda *args: None)
        assert post(client, PATH, action="begin").status_code == 303
        current = reviews.get("review-parent")
        assert current["state"] == "generating" and current["proposals"] == []
        response = submit(
            client,
            PATH,
            PATH,
            "patch-review",
            action="save",
            revision=draft["revision"],
            apply_cookie="on",
            prompt_cookie="이전 제안의 미저장 초안",
            value="not-retained",
            token="not-retained-either",
        )
        assert response.status_code == 303
        assert "not-retained" not in repr(client.app.state.form_flashes)
        page = client.get(response.headers["location"])
        assert "이전 제안의 미저장 초안</pre>" in page.text
        assert "<code>cookie</code>" in page.text
        assert 'http-equiv="refresh"' not in page.text
        assert reviews.get("review-parent") == current
