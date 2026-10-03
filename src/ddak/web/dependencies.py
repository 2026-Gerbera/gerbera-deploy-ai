"""FastAPI 라우터가 사용하는 내부 서비스와 템플릿."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException, Request

from ddak.core.defaults import (
    cloud_platform_default,
    load_aws_defaults,
    load_defaults,
    project_values,
)
from ddak.core.project_settings import ProjectSettings
from ddak.executor.engine import RunStatus
from ddak.executor.service import DeploymentService
from ddak.web.form_errors import form_context, form_error_for, form_return_to
from ddak.web.i18n import LocalizedTemplates
from ddak.web.narrative import ERRORS, PATTERNS, RULES, SOURCES, explain, short_summary, wording
from ddak.web.story import approval_story

ROOT = Path(__file__).resolve().parent


def navigation_context(request: Request) -> dict:
    app = request.scope.get("app")
    service = getattr(getattr(app, "state", None), "deployment", None)
    # 프로젝트 목록을 주지 않는 서비스(테스트 대역)도 현재 프로젝트만으로 사이드바를 그린다.
    reader = getattr(service, "list_projects", None)
    return {"sidebar_projects": reader() if reader is not None else []}


templates = LocalizedTemplates(
    directory=ROOT / "templates", context_processors=[navigation_context, form_context]
)
templates.env.policies["json.dumps_kwargs"] = {"sort_keys": True, "ensure_ascii": False}
templates.env.globals.update(
    form_error_for=form_error_for,
    form_return_to=form_return_to,
    approval_story=approval_story,
    step_wording=wording,
    narrative_explain=explain,
    short_summary=short_summary,
    narrative_errors=ERRORS,
    narrative_patterns=PATTERNS,
    narrative_rules=RULES,
    narrative_sources=SOURCES,
)


def deployment(request: Request) -> DeploymentService:
    service = request.app.state.deployment
    if service is None:
        raise RuntimeError("배포 서비스가 연결되지 않았다")
    return service


TERMINAL_STATUSES = frozenset({status.value for status in RunStatus} | {"SUPERSEDED"})


class ProjectRequired(HTTPException):
    """고를 프로젝트가 없다. HTML 화면은 프로젝트 생성 안내로 바꿔 보여 준다."""

    def __init__(self) -> None:
        super().__init__(404, "프로젝트를 먼저 만드세요. 프로젝트 설정에서 저장하면 선택됩니다")


def default_project(service: DeploymentService) -> str:
    """주소·실행환경 지정이 없을 때 최근 갱신된 저장 설정의 프로젝트를 고른다."""
    saved = service.list_project_settings()
    if not saved:
        raise ProjectRequired()
    # 감시 이관은 넘겨준 프로젝트도 같은 시각으로 갱신한다. 같은 시각이면 감시를 받은 쪽이 앞선다.
    latest = max(
        saved,
        key=lambda s: (s.get("updated_at") or 0, s.get("auto_detect") is not False, s["project"]),
    )
    return latest["project"]


def selected_project(request: Request, project: str | None = None) -> str:
    service = deployment(request)
    name = project or os.environ.get("DDAK_WATCH_PROJECT") or default_project(service)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
        raise HTTPException(400, "프로젝트 이름 형식 오류")
    return service.resolve_project(name)


def project_settings(request: Request, project: str) -> dict:
    saved = deployment(request).get_project_settings(project) or {}
    defaults = load_defaults()
    result = {
        **saved,
        **project_values(saved),
        "aws_expected_account_id": load_aws_defaults()["expected_account_id"],
        "setting_sources": {
            key: "관리 페이지"
            if saved.get(key) is not None
            else "기본 파일"
            if key in defaults
            else "기본 설정"
            for key in ProjectSettings.model_fields
        },
    }
    # 클라우드 플랫폼 이름: 관리 페이지 > 기본 파일 [cloud_platform] > 프로젝트 이름.
    # 저장값이 없으면 입력칸은 비우고 실제로 쓸 이름을 안내만 한다(기본값을 저장하지 않는다).
    platform = cloud_platform_default(project)
    result["cloud_platform_default"] = platform or project
    if saved.get("cloud_platform") is None and platform:
        result["setting_sources"]["cloud_platform"] = "기본 파일"
    base = getattr(request.app.state, "settings", None)
    if saved.get("aws_profile") is None and base is not None and base.aws_profile:
        result["aws_profile"] = base.aws_profile
        result["setting_sources"]["aws_profile"] = base.setting_sources.get(
            "aws_profile", "실행환경"
        )
    return result


def run_link(run: dict) -> str:
    status = run["status"]
    page = (
        "approval"
        if status == "AWAITING_APPROVAL"
        else "result"
        if status in TERMINAL_STATUSES
        else "progress"
    )
    return f"/runs/{run['run_id']}/{page}"


def public_links(context: dict) -> list[dict[str, str]]:
    """실행 스냅샷의 공개 주소만 표시한다. 자격증명·query·비 HTTP URL은 링크하지 않는다."""
    result = []
    targets = context.get("targets")
    addresses = []
    if targets != "cloud":
        addresses.append(("온프레미스", context.get("public_url")))
    if targets not in ("local", "onprem") and context.get("cloud_domain"):
        addresses.append(("클라우드", "https://" + context["cloud_domain"]))
    for label, raw in addresses:
        if not isinstance(raw, str) or any(c.isspace() or ord(c) < 32 for c in raw):
            continue
        try:
            url = urlsplit(raw)
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
            ):
                continue
            _ = url.port
        except ValueError:
            continue
        result.append(
            {
                "label": label,
                "url": raw,
                "version_url": urlunsplit((url.scheme, url.netloc, "/version", "", "")),
            }
        )
    return result


def watch_warnings(request: Request) -> list[str]:
    reader = getattr(request.app.state, "watch_warnings", None)
    return reader() if reader is not None else []


def live_version(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def project_runs(service: DeploymentService, project: str) -> list[dict]:
    recent = service.list_runs(limit=100, project=project)
    ids = {row["run_id"] for row in recent}
    pending = [row for row in service.list_pending_runs(project) if row["run_id"] not in ids]
    return sorted([*recent, *pending], key=lambda row: row["created"], reverse=True)


def preparation_failure(preparations: list[dict], runs: list[dict]) -> dict | None:
    if not preparations:
        return None
    latest = preparations[-1]
    if latest["status"] not in {"FAILED_BEFORE_DEPLOY", "CANCELLED"}:
        return None
    if not runs or latest.get("run_id") == runs[0]["run_id"]:
        return latest
    if not latest.get("run_id") and latest.get("created", 0) > runs[0]["created"]:
        return latest
    return None
