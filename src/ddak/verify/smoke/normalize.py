"""HTML 정규화(O3 문서 5-5 P1). 두 환경이 같은 이미지라면 같은 화면을 그려야 한다.

환경이 달라서 정상적으로 다른 것(주소, 날짜, 글 id, smoke 글 제목)을 표시로 바꾼 뒤 지문을 만든다.
- 두 환경의 DB 내용은 다르다(각자 글이 쌓인다). 그래서 글 목록 전체가 아니라
  "첫 글 앞부분"(머리·nav·오류 문구·v2 박스)과 "이번 run이 쓴 글" 하나만 비교한다.
  본문 상한(MAX_BODY)에서 잘려도 첫 글 앞부분은 남는다.
- 결과에는 원문이 아니라 짧은 지문(sha256 앞 16자)만 싣는다(관찰값은 짧은 스칼라, C-10).
"""

from __future__ import annotations

import hashlib
import re

# 앱(flaskr)은 스크립트·주석을 쓰지 않는다. 앞단 프록시(Cloudflare 터널의 봇 탐지·분석 스크립트,
# 이메일 가림 등)가 HTML에 넣을 수 있는 것이라 환경 차이로 보고 지운다
_INJECTED = re.compile(
    r"<script\b.*?</script\s*>|<noscript\b.*?</noscript\s*>|<!--.*?-->", re.I | re.S
)
# APP_BASE_URL을 그대로 쓰는 태그. 주소 자체가 예상된 차이(base_url)라 태그째 뺀다
_CANONICAL = re.compile(r"<link\b[^>]*\brel=[\"']?canonical\b[^>]*>", re.IGNORECASE)
_ORIGIN = re.compile(r"\bhttps?://[^/\s\"'<>]+", re.IGNORECASE)
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?\b")
_ID_PATH = re.compile(r"/\d+(?=[/\"'?#]|$)")
_ARTICLE = re.compile(r"<article\b.*?</article>", re.IGNORECASE | re.DOTALL)
_SPACE = re.compile(r"\s+")


def normalize_html(html: str, *, smoke_title: str | None = None) -> str:
    text = _CANONICAL.sub("", _INJECTED.sub("", html))
    if smoke_title:
        text = text.replace(smoke_title, "<SMOKE>")
    text = _ORIGIN.sub("<ORIGIN>", text)
    text = _DATE.sub("<DATE>", text)
    text = _ID_PATH.sub("/<ID>", text)
    text = _SPACE.sub(" ", text)
    return text.replace("> <", "><").strip()


def fingerprint(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:16]


def page_head(html: str, *, smoke_title: str | None = None) -> str | None:
    """첫 <article> 앞부분의 지문. 글이 하나도 없으면 None(비교할 기준이 없다)."""
    start = html.lower().find("<article")
    if start < 0:
        return None
    return fingerprint(normalize_html(html[:start], smoke_title=smoke_title))


def own_post(html: str, smoke_title: str) -> str | None:
    """이번 run이 쓴 글(<article>)의 지문. 목록에 없으면 None."""
    for match in _ARTICLE.finditer(html):
        if smoke_title in match.group(0):
            return fingerprint(normalize_html(match.group(0), smoke_title=smoke_title))
    return None
