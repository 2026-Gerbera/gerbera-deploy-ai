from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ddak.core.contracts.errors import DdakToolError
from ddak.core.project_settings import ProjectSettings
from ddak.web.dependencies import (
    deployment,
    project_settings,
    selected_project,
    templates,
    watch_warnings,
)
from ddak.web.domain import validate_domain_settings
from ddak.web.form_errors import FormError, FormRoute
from ddak.web.forms import parse_form
from ddak.web.routes.setup import transferred_names
from ddak.web.security import csrf_token, issue_csrf, require_safe_post

router = APIRouter(prefix="/settings", route_class=FormRoute)


@router.get("")
async def settings_page(request: Request, project: str | None = None):
    project = selected_project(request, project)
    current = project_settings(request, project)
    remember = getattr(deployment(request).store, "remember_settings_view", None)
    if remember is not None:
        current["settings_view"] = remember(
            project,
            current.get("version", 0),
            {key: current[key] for key in ProjectSettings.model_fields},
        )
    token = csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "project": project,
            "settings": current,
            "csrf_token": token,
            "watch_warnings": watch_warnings(request),
            "transferred_watchers": transferred_names(request.query_params.getlist("transferred")),
        },
    )
    issue_csrf(request, response, token)
    response.headers["Cache-Control"] = "no-store"
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
        saved = deployment(request).save_project_settings(
            project,
            {
                "repo_url": form.get("repo_url", "").strip() or None,
                "watch_branch": form.get("watch_branch", "prod").strip() or "prod",
                "auto_detect": form.get("auto_detect") == "on",
                "code_patch": form.get("code_patch") == "on",
                "default_targets": targets,
                "cloud_domain": domain,
                "dns_mode": dns_mode,
                "hosted_zone_id": zone,
                **(
                    {"aws_profile": form["aws_profile"].strip() or None}
                    if "aws_profile" in form
                    else {}
                ),
                **(
                    {"cloud_platform": form["cloud_platform"].strip() or None}
                    if "cloud_platform" in form
                    else {}
                ),
            },
            updated_by="local-operator",
            expected_version=expected,
            **({"view_token": form["settings_view"]} if form.get("settings_view") else {}),
        )
    except DdakToolError as exc:
        raise FormError(exc, 400) from None
    except ValueError:
        raise HTTPException(status_code=400, detail="설정 입력 형식 오류") from None
    params: dict[str, Any] = {"project": project, "saved": "1"}
    names = transferred_names(saved.get("transferred_watchers")) if isinstance(saved, dict) else []
    if names:
        params["transferred"] = names
    response = RedirectResponse("/settings?" + urlencode(params, doseq=True), status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/deploy")
async def request_deploy(request: Request):
    """계획 준비를 서비스에 맡기고 즉시 프로젝트 대시보드로 보낸다."""
    form = await parse_form(request)
    require_safe_post(request, form.get("csrf_token", ""))
    project = selected_project(request, form.get("project", "").strip() or None)
    try:
        deployment(request).enqueue_deployment(project)
    except DdakToolError as exc:
        raise FormError(exc, 409) from None
    except ValueError:
        raise HTTPException(status_code=409, detail="배포 요청 입력 형식 오류") from None
    return RedirectResponse(f"/?project={project}", status_code=303)
