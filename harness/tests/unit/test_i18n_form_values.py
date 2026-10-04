"""표시 언어를 바꿔도 재제출하는 폼 값과 저장 데이터는 원문을 유지한다."""

import copy
from html.parser import HTMLParser

import pytest

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.demo_reset import STAGES
from ddak.web.dependencies import templates
from ddak.web.translations_ja import translate
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_form_errors import submit
from tests.unit.test_i18n_ja2 import demo_view, remaining, request, review_view
from tests.unit.test_setup_web_fix11 import FakeCoordinator
from tests.unit.test_ui_integration_fix10 import PROJECT, client_for


class Controls(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = {}
        self.options = []
        self.textareas = {}
        self.textarea = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input":
            self.inputs.setdefault(attrs.get("name"), []).append(attrs)
        if tag == "option":
            self.options.append(attrs)
        if tag == "textarea":
            self.textarea = attrs.get("name")
            self.textareas[self.textarea] = ""

    def handle_endtag(self, tag):
        if tag == "textarea":
            self.textarea = None

    def handle_data(self, data):
        if self.textarea is not None:
            self.textareas[self.textarea] += data

    def value(self, name):
        return self.inputs[name][0]["value"]


def render(name, *, lang="ja", **context):
    req = request()
    req.scope["query_string"] = f"lang={lang}".encode()
    req.state.post_error = context.get("post_error")
    response = templates.TemplateResponse(
        request=req,
        name=name,
        context={
            "project": PROJECT,
            "csrf_token": "fixture",
            "settings": {},
            "submitted": {},
            "state": {"blocked_targets": [], "active_runs": []},
            **context,
        },
    )
    return response.body.decode()


@pytest.mark.parametrize("value", ["확인", "배포", '<확인 "배포">&'])
def test_saved_settings_values_remain_identical_in_both_languages(value):
    fields = (
        "repo_url",
        "watch_branch",
        "aws_profile",
        "cloud_platform",
        "cloud_domain",
        "hosted_zone_id",
        "settings_view",
    )
    settings = dict.fromkeys(fields, value)
    settings.update(
        version=2, default_targets="onprem", setting_sources={"aws_profile": "실행환경"}
    )
    before = copy.deepcopy(settings)
    for lang in ("ko", "ja"):
        page = render("settings.html", lang=lang, settings=settings)
        controls = Controls(page)
        assert all(controls.value(name) == value for name in fields)
        assert controls.value("version") == "2"
        if lang == "ja":
            assert "実行環境" in page
    assert settings == before


@pytest.mark.parametrize("value", ["확인", "배포", '<확인 "배포">&'])
def test_setup_values_options_and_inventory_keep_original(value):
    view = FakeCoordinator().data
    fields = (
        "generation_provider",
        "generation_model",
        "judgment_provider",
        "judgment_model",
        "image_repository",
        "buildx_builder",
        "git_author_name",
        "git_author_email",
        "aws_profile",
    )
    view["settings"].update(dict.fromkeys(fields, value))
    view["settings_view"] = value
    view["providers"] = [
        {
            "id": value,
            "label": "Provider",
            "kind": "api",
            "roles": ["generation", "judgment"],
            "models": [value],
            "default_model": value,
            "auth": "api_key",
        }
    ]
    before = copy.deepcopy(view)
    for lang in ("ko", "ja"):
        page = render(
            "setup.html",
            lang=lang,
            setup=view,
            project=value,
            csrf_token=value,
            submitted={"inventory": value, "key": value, "username": value},
        )
        controls = Controls(page)
        for name in (
            *fields[1:2],
            *fields[3:],
            "settings_view",
            "project",
            "csrf_token",
            "key",
            "username",
        ):
            assert controls.value(name) == value, name
        assert all(v["value"] == value for v in controls.inputs["provider_id"])
        assert sum(v.get("value") == value for v in controls.options) >= 4
        defaults = [v["data-default-model"] for v in controls.options if "data-default-model" in v]
        assert defaults and all(default == value for default in defaults)
        assert controls.textareas["inventory"] == value
    assert view == before


@pytest.mark.parametrize("field, value", [("ref", "배포"), ("reason", "확인")])
def test_failed_ops_post_restores_original_input(rig, monkeypatch, field, value):
    service, _, _ = rig
    method, route, form = (
        ("enqueue_deployment", "/ops/plan", "plan")
        if field == "ref"
        else ("unlock_project", "/ops/unlock", "unlock")
    )

    def fail(*args, **kwargs):
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "검사 실패")

    monkeypatch.setattr(service, method, fail)
    with client_for(service) as client:
        assert client.get(f"/ops?project={PROJECT}&lang=ja").status_code == 200
        response = submit(client, route, f"/ops?project={PROJECT}", form, **{field: value})
        assert response.status_code == 303
        page = client.get(response.headers["location"])
        assert 'lang="ja"' in page.text
        assert Controls(page.text).value(field) == value


def test_patch_values_are_raw_but_apply_label_is_japanese():
    review = review_view()
    review["items"][0].update(id="確認-id", title="배포")
    review["notes"] = {"確認-id": "확인"}
    review["candidate"] = {"id": "확인", "proposal": {"id": "other"}}
    page = render("patch_review.html", review=review, run_id="fixture", selection=[])
    controls = Controls(page)
    pick = controls.inputs["apply_確認-id"][0]
    assert pick["aria-label"] == "배포 適用"
    assert "data-i18n-raw" not in pick
    assert controls.value("candidate_id") == "확인"
    assert controls.textareas["prompt_確認-id"] == "확인"


@pytest.mark.parametrize(
    "stage",
    [
        *STAGES,
        "PR 닫힘(merge 안 됨)",
        "배포 진행 중",
        "배포 기록 성공 · 실제 서비스 확인 중",
        "배포 확인 필요 · FAILED_LOCAL",
        "배포 확인 필요 · FAILED_CLOUD",
        "v2 배포 완료",
        "v3 배포 완료",
    ],
)
def test_demo_stages_are_translated(stage):
    demo = demo_view()
    demo["actions"][0]["stage"] = stage
    page = render("_demo_state.html", demo=demo)
    assert remaining(page) == [], stage
    assert translate(stage) != stage
