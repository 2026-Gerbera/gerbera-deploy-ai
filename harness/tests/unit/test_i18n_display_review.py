"""출처·요약·생성문 표시 회귀. 저장 데이터와 외부 실행은 변경하지 않는다."""

import ast
import copy
import json
import re
from html import unescape
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import Environment

from ddak.core.contracts.tools.post_report import ReportFacts
from ddak.executor.reporting import fallback
from ddak.web.dependencies import templates
from ddak.web.display_language import display_short_summary, generated_in_korean
from ddak.web.narrative import short_summary
from ddak.web.routes.results import router
from ddak.web.story import approval_story, infra_story
from ddak.web.translations_ja import translate
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_ui_integration_fix10 import PROJECT, client_for, prepare
from tests.unit.test_ui_result_followup import recorded as recorded

SOURCES = {
    "인벤토리": "インベントリ",
    "배포 시 자동 생성·보관": "デプロイ時に自動生成・保管",
    "Terraform 출력": "Terraform出力",
    "관리 페이지 · 저장 여부는 연결 검사에서 확인": "管理画面 · 保存状況は接続検査で確認",
}
BADGE = 'data-generated-language="ko"'


def section(html, ident):
    return html.split(f'id="{ident}"', 1)[1].split("</section>", 1)[0]


def visible(html):
    return unescape(re.sub(r"<[^>]*>", "", html))


def japanese_template(name):
    templates.ja.env.globals.update(templates.env.globals)
    templates.ja.env.policies.update(templates.env.policies)
    return templates.ja.env.get_template(name)


def test_approval_with_environment_keys_translates_all_four_sources(rig, monkeypatch):
    service, source, _ = rig
    rid = prepare(service, source)
    display = service.get_display_data(rid)
    display["mappings"] = [
        {"key": "APP_MODE", "local": "인벤토리", "cloud": "Terraform 출력"},
        {
            "key": "APP_SIGNING_KEY",
            "local": "배포 시 자동 생성·보관",
            "cloud": "관리 페이지 · 저장 여부는 연결 검사에서 확인",
        },
    ]
    monkeypatch.setattr(service, "get_display_data", lambda _: display)
    before = copy.deepcopy((service.get_run(rid), display))
    with client_for(service) as client:
        response = client.get(f"/runs/{rid}/approval?lang=ja")
    assert response.status_code == 200 and 'lang="ja"' in response.text
    mapping = response.text.split('id="environment-mapping"', 1)[1].split("</table>", 1)[0]
    assert not re.search("[가-힣]", visible(mapping))
    assert "APP_MODE" in mapping and "APP_SIGNING_KEY" in mapping
    for korean, japanese in SOURCES.items():
        assert translate(korean) == japanese
        assert japanese in mapping
    assert (service.get_run(rid), display) == before


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED_CLOUD"])
def test_rule_fallback_report_is_japanese_and_unchanged(recorded, status):
    run, release, data = recorded
    run["status"] = status
    summary = fallback(ReportFacts(status=status))
    before = copy.deepcopy(summary)
    html = render_report(run, release, data, summary)
    report = section(html, "report-summary")
    assert not re.search("[가-힣]", visible(report))
    assert "変更ファイル0件を記録しました。" in report
    assert "イメージ0件のデプロイ情報を記録しました。" in report
    assert BADGE not in report and summary == before
    assert translate("변경 파일 12개를 기록했습니다.") == "変更ファイル12件を記録しました。"
    assert (
        translate("이미지 3개의 배포 정보를 기록했습니다.")
        == "イメージ3件のデプロイ情報を記録しました。"
    )
    assert translate("変更ファイル0件を記録しました。") == "変更ファイル0件を記録しました。"
    assert translate("原文: 변경 파일 2개를 기록했습니다.") == "原文: 변경 파일 2개를 기록했습니다."


def render_report(run, release, data, summary):
    service = SimpleNamespace(
        get_run=lambda _: run,
        get_release=lambda _: release,
        get_display_data=lambda _: data,
        events=lambda _: [],
        reports=SimpleNamespace(get=lambda _: summary),
    )
    app = FastAPI()
    app.state.deployment = service
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get(f"/runs/{run['run_id']}/result?lang=ja")
    assert response.status_code == 200 and 'lang="ja"' in response.text
    return response.text


@pytest.mark.parametrize("source", ["live", "cache", "replay"])
@pytest.mark.parametrize(
    ("line", "setting", "badge"),
    [
        ("B1 と verify.health.local、custom.check を確認しました。", "ko", False),
        ("배포 완료", "ja", True),
    ],
)
def test_report_badge_uses_actual_generated_text(recorded, source, line, setting, badge):
    run, release, data = recorded
    run["context"]["project_settings"] = {"ai_answer_language": setting}
    summary = {"state": "ready", "source": source, "narrative": {"conclusion": line, "changes": []}}
    before = copy.deepcopy((run, summary))
    report = section(render_report(run, release, data, summary), "report-summary")
    assert (BADGE in report) is badge
    if not badge:
        assert "投稿一覧の表示" in report and "サービス応答の確認" in report
        assert "確認処理" in report and not re.search("[가-힣]", visible(report))
    else:
        assert line in report
    assert (run, summary) == before
    summary["state"] = "pending"
    assert BADGE not in section(render_report(run, release, data, summary), "report-summary")


def test_summary_parser_preserves_korean_behavior_and_escapes_generated_markup():
    environment = Environment(autoescape=True)
    environment.globals["short_summary"] = display_short_summary
    template = environment.from_string("{{ short_summary(text)|e }}")
    text = "B1 verify.health.local custom.check <em>確認</em>"
    assert unescape(template.render(text=text, ui_language="ko")) == short_summary(text)
    rendered = template.render(text=text, ui_language="ja")
    assert "&lt;em&gt;確認&lt;/em&gt;" in rendered and "<em>" not in rendered
    assert not re.search("[가-힣]", rendered)
    assert (
        template.render(text='{"field": "raw"}', ui_language="ja") == "デプロイ記録を確認しました。"
    )
    assert template.render(text="", ui_language="ja") == "デプロイ記録を確認しました。"


def removal_rationale():
    path = (
        Path(__file__).resolve().parents[3] / "src/ddak/cloud/infra/tools/generate_infra/storage.py"
    )
    tree = ast.parse(path.read_text())
    return next(
        ast.literal_eval(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "rationale" for target in node.targets
        )
    )


@pytest.mark.parametrize("source", ["live", "cache", "replay"])
def test_storage_badge_and_fixed_removal_keep_raw_json(source):
    storage = {
        "intent": "create",
        "source": source,
        "rationale": [
            "한국어로 작성한 저장소 생성 근거입니다.",
            "배포 완료",
            "説明",
            "説明",
            "説明",
        ],
        "bucket": "display-fixture",
        "files": {"storage.tf": '# 기술 원문 "생성"'},
        "env": {"IMG_DIR": "원문값"},
        "evidence": [],
    }
    template = japanese_template("_resources.html")

    def render():
        return template.render(story={"infra": infra_story({"storage": storage})}, ui_language="ja")

    before = copy.deepcopy(storage)
    html = render()
    assert BADGE in html and storage["rationale"][0] in html
    assert "배포 완료" in html  # 생성문과 사전 문구가 같아도 원문을 유지한다.
    assert storage == before
    assert storage["files"]["storage.tf"] in unescape(html)
    assert (
        json.dumps(
            {"bucket": storage["bucket"], "env": storage["env"]},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        in html
    )
    storage["rationale"] = ["ストレージの生成理由です。"] * 5
    assert BADGE not in render()
    storage.update(intent="remove", rationale=list(removal_rationale()))
    html = render()
    assert BADGE not in html
    for sentence in storage["rationale"]:
        assert translate(sentence) != sentence
        assert translate(sentence) in html
        assert sentence not in html
    assert storage["files"]["storage.tf"] in unescape(html)


@pytest.mark.parametrize("source", ["live", "cache", "replay"])
def test_patch_badge_requires_visible_korean_reason(rig, source):
    service, root, _ = rig
    rid = prepare(service, root)
    view = service.approval_view(rid)
    view["project_settings"] = {"ai_answer_language": "ja"}
    reason = "필요한 변경을 규칙으로 보완"
    view["patch_meta"] = {"source": source, "reason": reason, "passed": True}
    template = japanese_template("approval.html")

    def render():
        return template.render(
            approval=view,
            story=approval_story(view),
            project=PROJECT,
            csrf_token="fixture",
            request={"url": {"path": "/approval"}},
            ui_language="ja",
        )

    before = copy.deepcopy(view)
    html = render()
    assert BADGE in section(html, "code-changes") and reason in html
    assert view == before
    raw = section(html, "approval-technical")
    assert json.dumps(view["patch_meta"], ensure_ascii=False, sort_keys=True, indent=2) in raw
    view["patch_meta"]["reason"] = "환경변수 읽기로 변경했습니다."
    assert BADGE in section(render(), "code-changes")
    for empty_or_japanese in ("", "必要な変更を提案しました。"):
        view["patch_meta"]["reason"] = empty_or_japanese
        view["project_settings"]["ai_answer_language"] = "ko"
        assert BADGE not in render()
    assert not generated_in_korean("rule", reason)
    assert not generated_in_korean("fixture", reason)
