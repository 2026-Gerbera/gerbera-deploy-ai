from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ddak.core.contracts.errors import DdakToolError
from ddak.core.project_settings import ProjectSettings
from ddak.core.redact import redact
from ddak.web.dependencies import (
    deployment,
    project_settings,
    selected_project,
    templates,
    watch_warnings,
)
from ddak.web.domain import validate_domain_settings
from ddak.web.forms import parse_form
from ddak.web.security import csrf_token, issue_csrf, require_safe_post

router = APIRouter(prefix="/settings")


@router.get("")
async def settings_page(request: Request, project: str | None = None):
    project = selected_project(request, project)
    current = project_settings(request, project)
    token = csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "project": project,
            "settings": current,
            "csrf_token": token,
            "watch_warnings": watch_warnings(request),
        },
    )
    issue_csrf(request, response, token)
    return response


@router.post("")
async def save_settings(request: Request):
    form = await parse_form(request)
    require_safe_post(request, form.get("csrf_token", ""))
    project = selected_project(request, form.get("project", "").strip() or None)
    try:
        targets = form.get("default_targets") or ProjectSettings().default_targets
        domain = form.get("cloud_domain", "").strip() or None
        dns_mode = form.get("dns_mode") or "external"
        zone = form.get("hosted_zone_id", "").strip() or None
        if targets != "onprem" or domain is not None:
            value = validate_domain_settings(domain or "", dns_mode, zone)
            domain, dns_mode, zone = value.cloud_domain, value.dns_mode, value.hosted_zone_id
        version_text = form.get("version", "")
        expected = int(version_text) if version_text else 0
        deployment(request).save_project_settings(
            project,
            {
                "repo_url": form.get("repo_url", "").strip() or None,
                "watch_branch": form.get("watch_branch", "prod").strip() or "prod",
                "auto_detect": form.get("auto_detect") == "on",
                "default_targets": targets,
                "cloud_domain": domain,
                "dns_mode": dns_mode,
                "hosted_zone_id": zone,
            },
            updated_by="local-operator",
            expected_version=expected,
        )
    except (DdakToolError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=redact(str(exc))) from exc
    return RedirectResponse(f"/settings?project={project}", status_code=303)


@router.post("/deploy")
async def request_deploy(request: Request):
    """계획 준비를 서비스에 맡기고 즉시 프로젝트 대시보드로 보낸다."""
    form = await parse_form(request)
    require_safe_post(request, form.get("csrf_token", ""))
    project = selected_project(request, form.get("project", "").strip() or None)
    try:
        deployment(request).enqueue_deployment(project)
    except (DdakToolError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=redact(str(exc))) from exc
    return RedirectResponse(f"/?project={project}", status_code=303)
