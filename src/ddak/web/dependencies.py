"""FastAPI 라우터가 사용하는 내부 서비스와 템플릿."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

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

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=ROOT / "templates", context_processors=[form_context])
templates.env.globals.update(form_error_for=form_error_for, form_return_to=form_return_to)


def deployment(request: Request) -> DeploymentService:
    service = request.app.state.deployment
    if service is None:
        raise RuntimeError("배포 서비스가 연결되지 않았다")
    return service


TERMINAL_STATUSES = frozenset({status.value for status in RunStatus} | {"SUPERSEDED"})


def selected_project(request: Request, project: str | None = None) -> str:
    name = project or os.environ.get("DDAK_WATCH_PROJECT") or "flaskr"
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
        raise HTTPException(400, "프로젝트 이름 형식 오류")
    return deployment(request).resolve_project(name)


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
        addresses.append(("온프렘", context.get("public_url")))
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
