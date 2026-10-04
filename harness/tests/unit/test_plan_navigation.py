"""source=fixture: 계획 조회 링크와 기존 실행·결과 이동의 렌더 회귀."""

import re
from html.parser import HTMLParser

import pytest

from ddak.web.dependencies import TERMINAL_STATUSES, templates
from ddak.web.narrative import pipeline_view
from ddak.web.story import result_story


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current = {**dict(attrs), "text": ""}
            self.links.append(self.current)

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag):
        if tag == "a":
            self.current = None


def render(name, **context):
    return templates.get_template(name).render(
        request={"url": {"path": "/fixture"}}, project="fixture", **context
    )


def heading_links(html):
    return Links(html.split('<div class="heading-actions">', 1)[1].split("</div>", 1)[0]).links


def assert_plan_link(links, run_id):
    plans = [link for link in links if link["text"] == "계획 보기"]
    assert len(plans) == 1
    assert plans[0]["href"] == f"/runs/{run_id}/approval"
    assert "hidden" not in plans[0] and "hidden" not in plans[0].get("class", "").split()
    return plans[0]


@pytest.mark.parametrize("status", ["APPROVED", "RUNNING", "SUCCEEDED", "FAILED_CLOUD"])
def test_progress_heading_links_to_current_plan_and_keeps_navigation(status):
    run = {"run_id": "progress-fixture", "status": status, "result": {}}
    html = render(
        "progress.html",
        run_id=run["run_id"],
        status=status,
        targets="both",
        terminal_states=sorted(TERMINAL_STATUSES),
        pipeline=pipeline_view(run, {}, []),
        story=result_story(run, None, {}),
        result={},
        environment_cards=[],
    )
    links = heading_links(html)
    assert_plan_link(links, run["run_id"])
    assert any(link["href"] == "/?project=fixture" for link in links)
    result = next(link for link in links if link.get("id") == "result-link")
    assert result["href"] == "/runs/progress-fixture/result"
    assert ("hidden" in result["class"].split()) == (status not in TERMINAL_STATUSES)


@pytest.mark.parametrize(
    "status,result,expected",
    [
        ("SUCCEEDED", {}, None),
        ("CANCELLED", {}, None),
        ("NEEDS_HUMAN", {}, ("운영 상태 확인", "/ops?project=fixture")),
        ("SUPERSEDED", {}, None),
        (
            "SUPERSEDED",
            {"superseded_by": "next-fixture"},
            ("새 실행 보기", "/runs/next-fixture/approval"),
        ),
        ("FAILED_BEFORE_DEPLOY", {"phase": "settings"}, ("설정 확인", "/settings?project=fixture")),
        ("FAILED_BEFORE_DEPLOY", {}, ("실패한 환경 확인", "/ops?project=fixture")),
        ("FAILED_LOCAL", {}, ("실패한 환경 확인", "/ops?project=fixture")),
        ("FAILED_CLOUD", {}, ("실패한 환경 확인", "/ops?project=fixture")),
        ("FAILED_VERIFY", {}, ("실패한 환경 확인", "/ops?project=fixture")),
        ("PARITY_FAILED", {}, ("실패한 환경 확인", "/ops?project=fixture")),
    ],
)
def test_result_plan_link_coexists_with_existing_next_action(status, result, expected):
    run = {"run_id": "result-fixture", "status": status, "result": result}
    html = render(
        "result.html",
        run=run,
        result=result,
        story=result_story(run, None, {}),
        environment_cards=[],
    )
    links = heading_links(html)
    plan = assert_plan_link(links, run["run_id"])
    assert "button" not in plan.get("class", "").split()
    actions = [(link["text"], link["href"]) for link in links if link is not plan]
    assert actions == ([expected] if expected else [])


@pytest.mark.parametrize("has_runs", [True, False])
def test_dashboard_plan_links_require_run_ids_and_keep_primary_row_links(has_runs):
    runs = (
        [
            {
                "run_id": "awaiting-fixture",
                "status": "AWAITING_APPROVAL",
                "url": "/runs/awaiting-fixture/approval",
                "preparation_counts": {},
            },
            {
                "run_id": "running-fixture",
                "status": "RUNNING",
                "url": "/runs/running-fixture/progress",
            },
            {"run_id": "done-fixture", "status": "SUCCEEDED", "url": "/runs/done-fixture/result"},
            {
                "run_id": "failed-fixture",
                "status": "FAILED_CLOUD",
                "url": "/runs/failed-fixture/result",
            },
            {
                "run_id": "superseded-fixture",
                "status": "SUPERSEDED",
                "url": "/runs/superseded-fixture/result",
            },
            {"status": "PREPARING", "url": "/?project=fixture"},
            {"run_id": None, "status": "FAILED_BEFORE_DEPLOY", "url": "/?project=fixture"},
            {"run_id": "", "status": "CANCELLED", "url": "/?project=fixture"},
        ]
        if has_runs
        else []
    )
    html = render(
        "dashboard.html",
        runs=[{**run, "sha": "abcdef0", "duration": None} for run in runs],
        preparations=[],
        settings={"repo_url": "https://example.com/fixture", "watch_branch": "prod"},
        state={"blocked_targets": [], "active_runs": []},
        csrf_token="fixture",
    )
    if not has_runs:
        assert "아직 실행 기록이 없습니다." in html
        assert not any(link["text"] == "계획 보기" for link in Links(html).links)
        return
    rows = re.findall(
        r"<tr>(.*?)</tr>", html.split("<tbody>", 1)[1].split("</tbody>", 1)[0], re.DOTALL
    )
    assert len(rows) == len(runs)
    for row, run in zip(rows, runs, strict=True):
        links = Links(row).links
        primary = [link for link in links if link["text"] != "계획 보기"]
        assert len(primary) == 2
        assert all(link["href"] == run["url"] for link in primary)
        if run.get("run_id"):
            assert_plan_link(links, run["run_id"])
        else:
            assert links == primary
