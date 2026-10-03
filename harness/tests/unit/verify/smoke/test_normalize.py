"""HTML 정규화: 환경 차이(주소·날짜·id·smoke 제목)는 지우고 화면 구조 차이는 남긴다."""

from __future__ import annotations

from ddak.verify.smoke.normalize import normalize_html, own_post, page_head

TITLE = "[smoke run-1]"


def test_normalize_replaces_environment_values() -> None:
    html = (
        '<link rel="canonical" href="https://app.example/">\n'
        '<a href="http://localhost:8080/12/update">  Edit </a> <span>2026-10-03 11:02:03</span>'
        f" <h1>{TITLE}</h1>"
    )
    assert normalize_html(html, smoke_title=TITLE) == (
        '<a href="<ORIGIN>/<ID>/update"> Edit </a><span><DATE></span><h1><SMOKE></h1>'
    )


def test_page_head_needs_an_article_and_ignores_the_tail() -> None:
    assert page_head("<h1>Posts</h1>") is None
    a = page_head("<nav>x</nav><article>1</article>" + "<article>old</article>" * 50)
    b = page_head("<nav>x</nav><article>2</article><artic")  # 본문 상한에서 잘린 경우
    assert a == b
    assert page_head("<nav>y</nav><article>1</article>") != a


def test_own_post_finds_this_runs_article_only() -> None:
    html = f"<article>other</article><article><h1>{TITLE}</h1> on 2026-10-03</article>"
    assert own_post(html, TITLE) == own_post(
        f"<article><h1>{TITLE}</h1> on 2026-10-04</article>", TITLE
    )
    assert own_post("<article>other</article>", TITLE) is None


def test_proxy_injected_scripts_and_comments_are_ignored() -> None:
    # 온프렘은 Cloudflare 터널을 거친다. 프록시가 넣는 스크립트·주석은 환경 차이다
    plain = "<nav>x</nav><article>1</article>"
    injected = (
        "<!-- cf --><nav>x</nav><script>window.__CF$cv$params={r:'1'}</script>"
        '<noscript><img src="/cdn-cgi/x"></noscript><article>1</article>'
    )
    assert page_head(injected) == page_head(plain)
