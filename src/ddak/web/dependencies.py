"""FastAPI 라우터가 사용하는 내부 서비스와 템플릿."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from ddak.core.project_settings import ProjectSettings
from ddak.executor.engine import RunStatus
from ddak.executor.service import DeploymentService
from ddak.web.form_errors import form_context, form_error_for, form_return_to

ROOT = Path(__file__).resolve().parent


def navigation_context(request: Request) -> dict:
    app = request.scope.get("app")
    service = getattr(getattr(app, "state", None), "deployment", None)
    return {"sidebar_projects": service.list_projects() if service is not None else []}


templates = Jinja2Templates(
    directory=ROOT / "templates", context_processors=[navigation_context, form_context]
)
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
    return {
        **ProjectSettings().model_dump(mode="json"),
        **(deployment(request).get_project_settings(project) or {}),
    }


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
