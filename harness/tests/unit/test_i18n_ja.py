"""언어 선택·표시 번역·승인 데이터 보존. 외부 서비스는 사용하지 않는다."""

import json
import re
from html.parser import HTMLParser

from fastapi import Request
from jinja2 import Environment
from markupsafe import Markup

from ddak.web import i18n
from ddak.web.dependencies import templates
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_ui_integration_fix10 import PROJECT, client_for, post, prepare


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        excluded = tag in {"script", "style", "template", "pre", "code"} or (
            "language-switch" in attrs.get("class", "")
            or attrs.get("id") == "report-summary"
            or attrs.get("id") == "approval-technical"
            or "story-technical" in attrs.get("class", "")
        )
        if tag not in {"meta", "link", "input", "br", "hr", "img"}:
            self.stack.append((tag, excluded))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        # 대시보드에 포함된 운영 폼은 PR-2 범위다. 일반 작업 details는 검사한다.
        if data.strip() in {"운영 도구", "運用ツール"}:
            self.stack = [(tag, excluded or tag == "details") for tag, excluded in self.stack]
        if not any(excluded for _, excluded in self.stack):
            self.parts.append(data)


def korean_visible(html):
    parser = VisibleText()
    parser.feed(html)
    return [p.strip() for p in parser.parts if re.search(r"[가-힣]", p)]


def test_language_cookie_toggle_and_four_pages(rig, monkeypatch):
    service, source, _ = rig
    monkeypatch.setenv("DDAK_WATCH_PROJECT", PROJECT)
    rid = prepare(service, source)
    with client_for(service) as client:
        ko = client.get(f"/?project={PROJECT}")
        assert 'lang="ko"' in ko.text and "배포 준비" in ko.text
        ja = client.get(f"/?project={PROJECT}&lang=ja")
        cookie = ja.headers["set-cookie"]
        assert all(
            part in cookie
            for part in ("ddak_lang=ja", "HttpOnly", "SameSite=strict", "Max-Age=2592000", "Path=/")
        )
        assert "Secure" not in cookie
        assert 'lang="ja"' in ja.text
        assert f"project={PROJECT}&amp;lang=ko" in ja.text
        assert not korean_visible(ja.text)
        approval = client.get(f"/runs/{rid}/approval")
        assert approval.status_code == 200 and not korean_visible(approval.text)
        assert approval.text == client.get(f"/ops/runs/{rid}/approval").text
        assert "&lt;span class=" not in approval.text
        service.approve(rid, approver="operator")
        progress = client.get(f"/runs/{rid}/progress")
        assert progress.status_code == 200 and not korean_visible(progress.text)
        service.store.finish(
            rid, "SUCCEEDED", {"tracks": {"local": "DONE", "cloud": "DONE"}}, {}, {}
        )
        result = client.get(f"/runs/{rid}/result")
        assert result.status_code == 200 and not korean_visible(result.text)
        assert 'lang="ko"' in client.get(f"/runs/{rid}/result?lang=ko").text


def test_finalize_preserves_markup_json_and_data_comparisons(tmp_path):
    source = """{% set mapping={'cloud':'출처 미확인'} %}
{% set file={'change':'삭제'} %}{% set role={'status':'변경 없음'} %}
{% if mapping['cloud'] == '출처 미확인' %}<b id="missing">{{ mapping.cloud }}</b>{% endif %}
{% if file.change == '삭제' %}<b id="deleted">{{ file.change }}</b>{% endif %}
{% if role.status == '변경 없음' %}<b id="same">{{ role.status }}</b>{% endif %}
<pre>{{ mapping|tojson }}</pre>{{ html }}"""
    (tmp_path / "example.html").write_text(source)
    loader = i18n.JapaneseLoader(tmp_path)
    env = Environment(loader=loader, autoescape=True, finalize=i18n.finalize_ja)
    translated_source = loader.get_source(env, "example.html")[0]
    assert "== '출처 미확인'" in translated_source
    assert "== '삭제'" in translated_source and "== '변경 없음'" in translated_source
    output = env.get_template("example.html").render(html=Markup('<span class="state">OK</span>'))
    for ident in ("missing", "deleted", "same"):
        assert f'id="{ident}"' in output
    assert '<span class="state">OK</span>' in output
    assert "\\ucd9c" in output  # tojson 원문은 번역하지 않는다.
    assert i18n.finalize_ja(1) == 1


def test_japanese_render_failure_falls_back_to_korean(rig, monkeypatch):
    service, source, _ = rig
    monkeypatch.setenv("DDAK_WATCH_PROJECT", PROJECT)
    prepare(service, source)
    monkeypatch.setattr(
        templates.ja,
        "TemplateResponse",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("broken")),
    )
    with client_for(service) as client:
        response = client.get("/?lang=ja")
        assert response.status_code == 200 and 'lang="ko"' in response.text
        assert "broken" not in response.text


def test_markup_error_macro_is_not_escaped():
    templates.ja.env.globals.update(templates.env.globals)
    template = templates.ja.env.get_template("_status.html")
    result = str(template.module.error_box({"code": "CONFIG_INVALID", "message": "설정 필요"}))
    assert '<section class="notice failure"' in result and "&lt;section" not in result


def test_language_saved_only_after_safe_post(rig, monkeypatch):
    service, _, _ = rig
    service.store.save_project_settings(
        PROJECT, {"auto_detect": False}, updated_by="operator", expected_version=0
    )
    before = service.get_project_settings(PROJECT)
    monkeypatch.setattr(
        service,
        "enqueue_deployment",
        lambda *a, **k: {"request_id": "fixture", "status": "PREPARING", "run_id": None},
    )
    with client_for(service) as client:
        client.get(f"/?project={PROJECT}&lang=ja")
        denied = client.post(
            "/ops/plan",
            data={"project": PROJECT, "csrf_token": "bad"},
            headers={"origin": "http://127.0.0.1:8765"},
        )
        assert denied.status_code == 403
        assert service.get_answer_language(PROJECT) == "ko"
        result = post(client, "/ops/plan", project=PROJECT)
        assert result.status_code == 303
        assert service.get_answer_language(PROJECT) == "ja"
        assert service.get_project_settings(PROJECT) == before


def test_post_language_link_uses_safe_get():
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/settings/deploy",
            "headers": [],
            "query_string": b"",
            "server": ("127.0.0.1", 8765),
            "scheme": "http",
        }
    )
    request.state.submitted_form = {"project": PROJECT}
    assert i18n.language_url(request, "ja") == f"/?project={PROJECT}&lang=ja"


def test_json_translation_is_opt_in():
    original = {"detail": "배포", "nested": ["승인"]}
    localized = i18n.translate_tree(original)
    assert localized == {"detail": "デプロイ", "nested": ["承認"]}
    assert json.loads(json.dumps(original)) == original


def test_language_middleware_keeps_stream_chunks_and_context():
    import asyncio

    from ddak.core.answer_language import request_language

    seen = []
    chunks = [b'data: {"status":"RUNNING"}\n\n', b"data: {}\n\n"]

    async def send(message):
        seen.append(message)

    async def app(scope, receive, send):
        assert request_language.get() == "ja"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": chunks[0], "more_body": True})
        assert seen[-1]["body"] == chunks[0]
        await send({"type": "http.response.body", "body": chunks[1], "more_body": False})

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/events",
        "query_string": b"lang=ja",
        "headers": [],
    }
    before = request_language.get()
    asyncio.run(i18n.LanguageMiddleware(app)(scope, None, send))
    assert request_language.get() == before
    assert [item["body"] for item in seen if "body" in item] == chunks
    assert any(name == b"set-cookie" for name, _ in seen[0]["headers"])


def test_invalid_query_uses_cookie_and_invalid_cookie_uses_korean():
    for cookie, expected in ((b"ddak_lang=ja", "ja"), (b"ddak_lang=other", "ko")):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "query_string": b"lang=other",
                "headers": [(b"cookie", cookie)],
            }
        )
        assert i18n.language(request) == expected


def test_existing_korean_report_is_labeled_and_not_retranslated(rig, monkeypatch):
    service, source, _ = rig
    rid = prepare(service, source)
    service.store.finish(rid, "SUCCEEDED", {"tracks": {"local": "DONE"}}, {}, {})
    monkeypatch.setattr(
        service.reports,
        "get",
        lambda _: {
            "state": "ready",
            "source": "ai",
            "narrative": {
                "conclusion": "온프레미스",
                "changes": [],
                "checks": [],
                "next_action": "",
            },
        },
    )
    with client_for(service) as client:
        response = client.get(f"/runs/{rid}/result?lang=ja")
    assert response.status_code == 200 and 'lang="ja"' in response.text
    assert 'data-generated-language="ko">韓国語で生成' in response.text
    assert "<li>온프레미스</li>" in response.text
