"""B안 통합 이후에도 설정·승인 기능은 같은 데이터와 POST 경계를 쓴다."""

from ddak.web.dependencies import templates
from tests.unit.test_deployment_service import patch_metadata
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_setup_web_fix11 import FakeCoordinator
from tests.unit.test_ui_integration_fix10 import PROJECT, client_for, post, prepare
from tests.unit.test_ui_redesign import Forms


def test_settings_preserves_enabled_patch_when_saving_other_fields(rig):
    service, _, _ = rig
    service.save_project_settings(
        PROJECT,
        {"code_patch": True, "repo_url": "https://github.com/example/app.git"},
        updated_by="fixture",
        expected_version=0,
    )
    with client_for(service) as client:
        page = client.get(f"/settings?project={PROJECT}")
        assert 'name="code_patch" type="checkbox" checked' in page.text
        assert f'href="/setup?project={PROJECT}"' in page.text
        result = post(
            client,
            "/settings",
            project=PROJECT,
            version="1",
            repo_url="https://github.com/example/updated.git",
            code_patch="on",
            default_targets="onprem",
        )
        assert result.status_code == 303
        page = client.get(result.headers["location"])
        assert 'name="code_patch" type="checkbox" checked' in page.text
        assert "https://github.com/example/updated.git" in page.text


def test_dashboard_setup_checks_and_project_links_survive_redesign(rig):
    service, _, _ = rig
    service.onboarding = FakeCoordinator()
    with client_for(service) as client:
        html = client.get(f"/?project={PROJECT}").text
        assert "연결 상태 점검표" in html and "push 권한 없음" in html
        assert 'class="state failure"' in html and "미확인" in html
        assert f'href="/setup?project={PROJECT}"' in html
        ops = client.get(f"/ops?project={PROJECT}").text
        assert f'href="/setup?project={PROJECT}"' in ops


def test_patch_explanation_and_decision_basis_display_without_raw_diff(rig):
    service, source, _ = rig
    rid = prepare(service, source)
    view = service.approval_view(rid)
    view.update(
        patch="private-" + "source-sentinel",
        missing_env_keys=["APP_BASE_URL"],
        patch_meta={
            **patch_metadata(reason="개발 설정을 필수 환경변수로 전환"),
            "gitleaks": "passed",
            "new_env_keys": ["APP_BASE_URL"],
            "patterns": ["loopback"],
            "target_hashes": {"app.py": "b" * 64},
        },
        decision_basis={
            "planner": {"provider": "fixture-provider", "model": "fixture-model"},
            "steps": [{"evidence": ["fact:tree_changed.was"]}],
            "skipped": [{"reason": "unchanged web"}],
            "invalidated": [{"reason": "required step restored"}],
        },
    )
    html = templates.get_template("approval.html").render(
        approval=view,
        project=PROJECT,
        csrf_token="fixture",
        request={"url": {"path": "/approval"}},
    )
    visible = html.split("<details>")[0]
    # 판정 근거 요약은 화면에 보이고, 단계별 근거·제외·무효화 원문은 접힌 기술 정보에 둔다.
    technical = html.split('<details id="approval-technical">', 1)[1].split("</details>", 1)[0]
    for text in (
        "check_patch: 통과",
        "새 환경 키: APP_BASE_URL",
        "값 필요",
        "loopback",
        "b" * 64,
        "계획 판정 근거",
        "fixture-provider · fixture-model",
        'data-label="포함 단계">1개',
        'data-label="제외 단계">1개',
    ):
        assert text in visible
    assert "계획 판정 근거 원문" in technical
    for text in (
        "fixture-provider",
        "fact:tree_changed.was",
        "unchanged web",
        "required step restored",
    ):
        assert text in technical
    for raw in ("fact:tree_changed.was", "unchanged web", "required step restored"):
        assert raw not in visible
    assert view["patch"] not in html
    forms = Forms()
    forms.feed(html)
    assert len(forms.forms) == 1
    assert {"csrf_token", "project"} <= forms.forms[0]["fields"]


def test_setup_badges_use_theme_without_claiming_deployment_success():
    data = FakeCoordinator().data
    data["states"]["custom"] = {"status": "green", "detail": "연결 확인됨"}
    html = templates.get_template("setup.html").render(
        setup=data, project=PROJECT, csrf_token="fixture", request={"url": {"path": "/setup"}}
    )
    assert 'class="state success" data-status="green"' in html
    assert 'class="state failure" data-status="red"' in html
    assert 'class="state neutral" data-status="gray"' in html
    assert "배포 완료" not in html
