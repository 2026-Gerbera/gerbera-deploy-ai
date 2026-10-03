"""source=fixture: 클라우드 플랫폼 이름 설정의 저장·검증·기본값·화면. 외부 실행 없음."""

from __future__ import annotations

from types import SimpleNamespace

import pydantic
import pytest
from fastapi import HTTPException

from ddak import app
from ddak.core import defaults
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.project_settings import ProjectSettings
from ddak.core.store import Store
from ddak.plan.intake import FetchPolicy, WatchTarget
from ddak.web.routes import settings
from tests.unit import test_deployment_service as support
from tests.unit.test_ui_settings_fix10 import FormMarkup, Service, request
from tests.unit.test_ui_settings_fix10 import service as service  # pytest fixture 재사용

rig = support.rig
pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("value", ["flaskr", "flaskr-three", "a" * 40])
def test_project_settings_accepts_cloud_name(value):
    assert ProjectSettings(cloud_platform=value).cloud_platform == value


@pytest.mark.parametrize("value", ["Flaskr", "flaskr_x", "-flaskr", "1flaskr", "a" * 41, ""])
def test_project_settings_rejects_bad_cloud_name(value):
    with pytest.raises(pydantic.ValidationError):
        ProjectSettings(cloud_platform=value)


def test_store_saves_and_validates_cloud_platform(tmp_path):
    store = Store(tmp_path / "settings.sqlite")
    saved = store.save_project_settings(
        "flaskr-three", {"cloud_platform": "flaskr"}, updated_by="test", expected_version=0
    )
    assert store.project_settings("flaskr-three")["cloud_platform"] == "flaskr"
    with pytest.raises(pydantic.ValidationError):
        store.save_project_settings(
            "flaskr-three",
            {"cloud_platform": "flaskr_bad"},
            updated_by="test",
            expected_version=saved["version"],
        )
    assert store.project_settings("flaskr-three")["cloud_platform"] == "flaskr"


def test_defaults_file_maps_only_listed_project():
    assert defaults.cloud_platform_default("flaskr-three") == "flaskr"
    assert defaults.cloud_platform_default("flaskr") is None
    assert defaults.cloud_platform_default("other") is None
    # 전역 [project] 기본값에는 들어가지 않는다(모든 프로젝트가 같은 플랫폼을 쓰게 되므로).
    assert "cloud_platform" not in defaults.load_defaults()
    assert defaults.project_values({})["cloud_platform"] is None


@pytest.mark.parametrize(
    "text,reader",
    [
        ('[project]\ncloud_platform = "flaskr"\n', "load_defaults"),
        ('[project]\n[cloud_platform]\nflaskr-three = "Bad_Name"\n', "cloud_platform_default"),
    ],
)
def test_defaults_file_rejects_global_or_bad_platform(monkeypatch, tmp_path, text, reader):
    path = tmp_path / "defaults.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(defaults, "DEFAULTS_PATH", path)
    read = getattr(defaults, reader)
    with pytest.raises(ValueError):
        read("flaskr-three") if reader == "cloud_platform_default" else read()


async def test_settings_page_shows_platform_default_and_source(service: Service) -> None:
    response = await settings.settings_page(request(service), "flaskr-three")
    field = FormMarkup(response.body.decode()).fields["cloud_platform"]
    current = response.context["settings"]
    assert field["value"] == ""
    assert field["placeholder"] == "flaskr"
    assert current["setting_sources"]["cloud_platform"] == "기본 파일"
    assert '<small data-setting-source="cloud_platform">출처: 기본 파일</small>' in (
        response.body.decode()
    )

    response = await settings.settings_page(request(service), "other")
    field = FormMarkup(response.body.decode()).fields["cloud_platform"]
    assert field["placeholder"] == "other"
    assert response.context["settings"]["setting_sources"]["cloud_platform"] == "기본 설정"

    service.store.rows["other"] = {"cloud_platform": "flaskr", "version": 1}
    response = await settings.settings_page(request(service), "other")
    field = FormMarkup(response.body.decode()).fields["cloud_platform"]
    assert field["value"] == "flaskr"
    assert response.context["settings"]["setting_sources"]["cloud_platform"] == "관리 페이지"


@pytest.mark.parametrize("submitted,expected", [("flaskr", "flaskr"), ("  ", None)])
async def test_settings_form_saves_cloud_platform(
    service: Service, submitted: str, expected: str | None
) -> None:
    form = {"project": "flaskr-three", "cloud_platform": submitted}
    response = await settings.save_settings(request(service, form=form))
    assert response.status_code == 303
    assert service.store.writes[0]["data"]["cloud_platform"] == expected


async def test_settings_form_rejects_bad_cloud_platform(service: Service) -> None:
    form = {"project": "flaskr-three", "cloud_platform": "Flaskr_Three"}
    with pytest.raises(HTTPException) as exc:
        await settings.save_settings(request(service, form=form))
    assert exc.value.status_code == 400
    assert service.store.writes == []


@pytest.mark.parametrize(
    "saved,expected", [({}, "flaskr"), ({"cloud_platform": "platform-b"}, "platform-b")]
)
async def test_run_snapshot_pins_cloud_platform(rig, monkeypatch, saved, expected):
    service, source, _ = rig
    monkeypatch.setattr(app, "cloud_platform_default", {"demo": "flaskr"}.get)
    monkeypatch.setenv("DDAK_ONPREM_INVENTORY", "/fixture/inventory.yaml")
    monkeypatch.setattr(app, "load_inventory", lambda path: {"public_url": "https://o.example"})
    service.save_project_settings(
        "demo",
        {"cloud_domain": "demo.example.test", "default_targets": "cloud", **saved},
        updated_by="operator",
        expected_version=0,
    )

    def plan(request, **kwargs):
        p = support.plan(kwargs["run_id"]).model_copy(update={"mode": request.mode})
        ctx = RunContext(p.run_id, project=p.project, mode=p.mode)
        return SimpleNamespace(plan=p, context=ctx, source=source)

    monkeypatch.setattr(app, "plan_deployment", plan)
    rid = await app._prepare_commit(
        service,
        Settings(),
        WatchTarget("demo", "https://github.com/org/app", "prod", "both"),
        "a" * 40,
        policy=FetchPolicy(root=source.parent),
    )
    assert service.approval_view(rid)["project_settings"]["cloud_platform"] == expected
