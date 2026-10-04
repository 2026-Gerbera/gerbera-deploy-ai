"""source=fixture: 공통 코드 카드의 가림 토큰·생략 행·원문 보존을 검사한다."""

import copy
import re
from html import unescape
from pathlib import Path

import pytest
from fastapi import Request
from markupsafe import escape

from ddak.web.i18n import LocalizedTemplates

TOKENS = (
    ("[가림 · 코드 줄]", "[非表示 · コード行]"),
    ("[가림 · 문자열]", "[非表示 · 文字列]"),
    ("[가림 · 비밀값]", "[非表示 · 秘密値]"),
    ("[가림 · 주소]", "[非表示 · アドレス]"),
    ("[가림 · 숫자]", "[非表示 · 数値]"),
    ("[가림 · 주석]", "[非表示 · コメント]"),
)
PAGES = ("approval.html", "patch_review.html")


@pytest.fixture
def templates():
    engine = LocalizedTemplates(
        Path(__file__).resolve().parents[3] / "src/ddak/web/templates", context_processors=[]
    )
    engine.env.globals.update(narrative_errors={}, narrative_patterns={})
    return engine


def render(templates, name, cards, lang):
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/runs/fixture/approval"
            if name == "approval.html"
            else "/runs/fixture/patch-review",
            "headers": [],
            "query_string": f"lang={lang}".encode(),
            "scheme": "http",
            "server": ("localhost", 80),
        }
    )
    item = {
        "id": "entry",
        "title": "Fixture proposal",
        "reason": "Fixture reason",
        "revision": 1,
        "changes": cards,
        "required": False,
    }
    context = {
        "project": "fixture",
        "csrf_token": "fixture",
        "submitted": {},
        "run_id": "fixture",
        "selection": ["entry"],
        "approval": {"project": "fixture", "run_id": "fixture", "subjects": {}},
        "story": {
            "targets": [],
            "files": [],
            "findings": [],
            "patches": cards,
            "mappings": [],
            "plan_rows": [],
            "images": [],
            "containers": [],
            "infra": {"available": False},
        },
        "review": {
            "state": "ready",
            "revision": 1,
            "source": "fixture",
            "items": [item],
            "candidate": {"id": "candidate", "proposal": item, "changes": cards},
        },
    }
    response = templates.TemplateResponse(request=request, name=name, context=context)
    page = response.body.decode()
    assert response.status_code == 200
    assert f'<html lang="{lang}">' in page
    articles = re.findall(r'<article class="code-change">(.*?)</article>', page, re.S)
    assert len(articles) == len(cards) * (2 if name == "patch_review.html" else 1)
    return articles


def card(rows):
    return {"file": "app.py", "line": 77, "end": 83, "rows": rows}


@pytest.mark.parametrize("name", PAGES)
def test_code_masks_translate_in_both_pages_without_mutating_cards(templates, name):
    original = '\t값 = "' + " | ".join(korean for korean, _ in TOKENS) + '"  # 확인\r\n'
    japanese = '\t값 = "' + " | ".join(japanese for _, japanese in TOKENS) + '"  # 확인\r\n'
    cards = [
        card(
            [
                {"kind": "ctx", "old": 77, "new": 77, "text": "배포 완료"},
                {"kind": "del", "old": 80, "new": None, "text": original},
                {"kind": "add", "old": None, "new": 80, "text": original},
            ]
        )
    ]
    before = copy.deepcopy(cards)
    for lang in ("ja", "ko", "ja", "ko"):
        expected = japanese if lang == "ja" else original
        for article in render(templates, name, cards, lang):
            codes = re.findall(r"<div><code>(.*?)</code>", article, re.S)
            assert codes == list(map(escape, ["배포 완료", expected, expected]))
            assert [unescape(code).encode() for code in codes] == [
                text.encode() for text in ("배포 완료", expected, expected)
            ]
            assert '<li class="ctx"><span class="numeric">77</span>' in article
            assert '<li class="del"><span class="numeric">80</span>' in article
            assert '<li class="add"><span class="numeric">80</span>' in article
            assert '<span aria-hidden="true">\u2212</span>' in article
            assert '<span aria-hidden="true">+</span>' in article
            assert f"app.py 77\u201383{'行' if lang == 'ja' else '행'}" in article
        assert cards == before


@pytest.mark.parametrize("name", PAGES)
@pytest.mark.parametrize("lang", ("ko", "ja"))
def test_gap_is_one_row_without_line_number_or_diff_marker(templates, name, lang):
    cards = [
        card(
            [
                {"kind": "ctx", "old": 77, "new": 77, "text": "pass"},
                {"kind": "gap", "count": 5, "old": None, "new": None, "text": ""},
                {"kind": "ctx", "old": 83, "new": 83, "text": "pass"},
            ]
        )
    ]
    for article in render(templates, name, cards, lang):
        rows = re.findall(r'<li class="(.*?)">(.*?)</li>', article, re.S)
        assert [kind for kind, _ in rows] == ["ctx", "gap", "ctx"]
        gap = rows[1][1]
        expected = "… 5行省略" if lang == "ja" else "… 5줄 생략"
        assert unescape(re.sub(r"<[^>]*>", "", gap)) == expected
        assert '<span class="numeric"></span>' in gap
        assert '<span aria-hidden="true"></span>' in gap
        assert "<code>" not in gap and "<small>" not in gap


@pytest.mark.parametrize("name", PAGES)
@pytest.mark.parametrize("lang", ("ko", "ja"))
def test_other_code_is_preserved_and_html_is_escaped(templates, name, lang):
    texts = [
        "배포 완료",
        "가림",
        "문자열 비밀값 주소 숫자 주석 코드 줄",
        "[가림·문자열] [가림 · 문자열 ] [가림 ·  문자열] [가림 · 알 수 없음]",
        "[가림 · 코드줄] [가림 · 환경 값] [가림 · 문자열: 값] [가림 · 주석",
        "[非表示 · 文字列]",
        '\t<em title="확인">가림</em> & &#60;script&#62; {{ 확인 }}\r\n',
        '</code><script>alert("fixture")</script><code>',
    ]
    cards = [
        card(
            [{"kind": "ctx", "old": i, "new": i, "text": text} for i, text in enumerate(texts, 77)]
        )
    ]
    for article in render(templates, name, cards, lang):
        codes = re.findall(r"<div><code>(.*?)</code>", article, re.S)
        assert codes == list(map(escape, texts))
        assert [unescape(code).encode() for code in codes] == [text.encode() for text in texts]
        assert "<script>" not in article and "<em" not in article
