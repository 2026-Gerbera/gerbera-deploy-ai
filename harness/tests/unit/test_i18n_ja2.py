"""관리 화면 전체의 표시 문구와 원문 보존 경계를 오프라인으로 검사한다."""

import copy
import json
import re
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from ddak.core.config import Settings
from ddak.web.app import create_app
from ddak.web.dependencies import templates
from ddak.web.routes import ops, setup, setup_actions
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_setup_web_fix11 import FakeCoordinator
from tests.unit.test_ui_integration_fix10 import BASE, PROJECT, prepare

HANGUL = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")


class DisplayPhrases(HTMLParser):
    """언어 선택기와 명시한 기술/생성 원문만 제외한 표시 텍스트·속성 목록."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.remaining = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        raw = tag in {"script", "style", "pre", "code"} or (
            "data-i18n-raw" in attrs or "language-switch" in attrs.get("class", "")
        )
        raw = raw or any(item[1] for item in self.stack)
        if not raw:
            for name in (
                "title",
                "alt",
                "placeholder",
                "aria-label",
                "aria-description",
                "data-label",
                "data-confirm",
            ):
                self.add(attrs.get(name, ""))
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append((tag, raw or tag == "textarea"))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, value):
        if not any(item[1] for item in self.stack):
            self.add(value)

    def add(self, value):
        if HANGUL.search(value):
            self.remaining.add(" ".join(value.split()))


def remaining(html):
    parser = DisplayPhrases()
    parser.feed(html)
    return sorted(parser.remaining)


def demo_view():
    return {
        "available_tags": ["v1", "v2", "v3"],
        "warning": "",
        "environments": [
            {
                "label": "온프레미스",
                "version": "v1",
                "detail": "응답 확인",
                "sha": "a" * 40,
                "url": "https://example.test",
                "response": {"fixture": "기술 원문"},
            }
        ],
        "actions": [
            {
                "action": "prepare-v3",
                "tag": "v3",
                "stage": "PR 열림",
                "url": "https://example.test/pr/1",
                "number": 1,
                "run_id": "",
                "already_source": False,
            }
        ],
    }


def review_view():
    return {
        "state": "ready",
        "busy": False,
        "revision": 1,
        "selected": ["entry"],
        "source": "replay",
        "warnings": [],
        "error": None,
        "notes": {},
        "proposals": [{"id": "entry"}],
        "candidate": None,
        "items": [
            {
                "id": "entry",
                "title": "Existing proposal",
                "reason": "원문 보존 근거",
                "revision": 1,
                "changes": [],
                "required": False,
                "requires": [],
                "env_vars": ["APP_MODE"],
            }
        ],
    }


def test_all_page_routes_in_japanese_have_no_untranslated_display(rig, tmp_path):
    service, source, _ = rig
    service.save_project_settings(
        PROJECT,
        {"repo_url": "https://example.test/app", "auto_detect": False},
        updated_by="operator",
        expected_version=0,
    )
    rid = prepare(service, source)
    coordinator = FakeCoordinator()
    coordinator.data["checklist"] = [
        {
            "id": "repository",
            "label": "앱 저장소 push 권한",
            "status": "gray",
            "detail": "실제 연결 확인 전",
        }
    ]
    service.onboarding = coordinator
    service.setup_actions = SimpleNamespace(
        view=lambda _: {
            "actions": [
                {
                    "id": "fixture",
                    "kind": "database",
                    "status": "AWAITING_APPROVAL",
                    "detail": "승인 대기",
                    "hash": "b" * 64,
                    "summary": "기술 원문",
                }
            ]
        }
    )
    review = review_view()
    service.patch_reviews = SimpleNamespace(
        get=lambda _: review, view=lambda _: review, shutdown=AsyncMock()
    )
    service.demo_reset = SimpleNamespace(view=lambda _: demo_view())
    application = create_app(deployment_factory=lambda: service, settings=Settings())
    for router in (ops.router, setup.router, setup_actions.router):
        application.include_router(router)
    inventory = {}
    with TestClient(application, base_url=BASE) as client:
        for path in (
            "/",
            "/projects",
            "/settings",
            "/setup",
            "/setup/actions",
            "/ops",
            "/ops/demo/status",
            f"/runs/{rid}/patch-review",
            f"/runs/{rid}/approval",
            f"/runs/{rid}/progress",
        ):
            response = client.get(path, params={"project": PROJECT, "lang": "ja"})
            assert response.status_code == 200, (path, response.text[:300])
            if path != "/ops/demo/status":
                assert 'lang="ja"' in response.text, path
            inventory[path] = remaining(response.text)
        service.store.finish(rid, "SUCCEEDED", {"tracks": {"local": "DONE"}}, {}, {})
        response = client.get(f"/runs/{rid}/result?lang=ja")
        assert response.status_code == 200
        inventory["result"] = remaining(response.text)
    (tmp_path / "japanese-remaining.json").write_text(json.dumps(inventory, ensure_ascii=False))
    assert not any(inventory.values()), json.dumps(inventory, ensure_ascii=False, indent=2)


def request(path="/setup"):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"lang=ja",
            "scheme": "http",
            "server": ("127.0.0.1", 8765),
            "app": SimpleNamespace(state=SimpleNamespace()),
        }
    )


def render(name, **context):
    response = templates.TemplateResponse(
        request=request(),
        name=name,
        context={
            "project": PROJECT,
            "csrf_token": "fixture",
            "settings": {},
            "submitted": {},
            **context,
        },
    )
    assert response.status_code == 200
    return response.body.decode()


@pytest.mark.parametrize(
    "name, context",
    [
        ("project_required.html", {}),
        ("overview.html", {"projects": [], "project_required": True}),
        ("approval_unavailable.html", {"run_id": "fixture", "detail": "배포 완료"}),
        ("form_failure.html", {"error": {"code": "CONFIG_INVALID", "message": "배포 완료"}}),
        ("patch_review.html", {"run_id": "fixture", "review": None}),
        (
            "patch_review.html",
            {
                "run_id": "fixture",
                "review": {
                    "state": "patch_lost",
                    "loss_locations": [{"file": "app.py", "line": 3}, {"file": "config.py"}],
                },
            },
        ),
        (
            "patch_review.html",
            {"run_id": "fixture", "blocked": True, "review": None, "error": "배포 완료"},
        ),
        *[
            ("patch_review.html", {"run_id": "fixture", "review": {"state": state, "busy": True}})
            for state in ("generating", "reviewing", "finalizing", "publishing")
        ],
    ],
)
def test_empty_failed_and_busy_pages(name, context):
    before = copy.deepcopy(context)
    html = render(name, **context)
    assert 'lang="ja"' in html
    assert remaining(html) == []
    assert context == before
    if name in {"form_failure.html", "approval_unavailable.html"} or context.get("error"):
        assert "배포 완료" in html  # 오류 원문은 사전과 일치해도 번역하지 않는다.


def test_all_status_badges_and_stale_form_macro_translate():
    from ddak.web.narrative import ERRORS

    templates.ja.env.globals.update(templates.env.globals)
    module = templates.ja.env.get_template("_status.html").module
    for state in module.labels:
        assert remaining(str(module.state(state))) == [], state
    for code in ERRORS:
        html = str(module.error_box({"code": code, "message": "배포 완료"}))
        assert remaining(html) == [], code
        assert "배포 완료" in html


def test_patch_candidates_and_stale_form_keep_original_text():
    review = review_view()
    review["warnings"] = ["배포 완료"]
    review["items"][0]["requires"] = ["existing"]
    review["items"].append(
        {**review["items"][0], "id": "existing", "required": True, "requires": []}
    )
    review["candidate"] = {
        "id": "candidate",
        "proposal": {"id": "entry", "reason": "배포 완료"},
        "changes": [],
    }
    review["prompt"] = "배포 완료"
    html = render("patch_review.html", run_id="fixture", review=review, selection=["entry"])
    assert remaining(html) == []
    assert "배포 완료" in html
    module = templates.ja.env.get_template("_status.html").module
    html = str(
        module.error_box(
            {
                "code": "PRECONDITION_FAILED",
                "message": "배포 완료",
                "submitted": {"revision": "1", "apply_entry": "on", "prompt_entry": "배포 완료"},
            }
        )
    )
    assert remaining(html) == []
    assert "<pre>배포 완료</pre>" in html


def test_loader_translates_attributes_after_expression_without_changing_data(tmp_path):
    from jinja2 import Environment

    from ddak.web.i18n import JapaneseLoader

    original = (
        '<input name="ref" value="{{ ref }}" placeholder="비우면 배포 브랜치의 최신 커밋">'
        '<textarea name="prompt_{{ item.id }}" placeholder="기본 설정 유지"></textarea>'
        '<input value="기본 설정 유지" data-value="기본 설정 유지">'
        '{{ "기본 설정 유지" }}'
    )
    (tmp_path / "attribute.html").write_text(original)
    loader = JapaneseLoader(tmp_path)
    translated = loader.get_source(Environment(loader=loader, autoescape=True), "attribute.html")[0]
    assert 'placeholder="空欄の場合はデプロイブランチの最新コミット"' in translated
    assert 'placeholder="基本設定を維持"' in translated
    assert 'value="{{ ref }}"' in translated
    assert 'name="prompt_{{ item.id }}"' in translated
    assert 'value="기본 설정 유지" data-value="기본 설정 유지"' in translated
    assert '{{ "기본 설정 유지" }}' in translated
