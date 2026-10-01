import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ddak.core.contracts.errors import DdakToolError
from ddak.web.dependencies import deployment, templates
from ddak.web.domain import validate_domain_settings
from ddak.web.forms import parse_form
from ddak.web.security import csrf_token, issue_csrf, require_safe_post

router = APIRouter(prefix="/settings")


@router.get("")
async def settings_page(request: Request, project: str = "flaskr"):
    current = deployment(request).store.project_settings(project)
    token = csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"project": project, "settings": current or {}, "csrf_token": token},
    )
    issue_csrf(request, response, token)
    return response


@router.post("")
async def save_settings(request: Request):
    form = await parse_form(request)
    require_safe_post(request, form.get("csrf_token", ""))
    project = form.get("project", "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", project):
        raise HTTPException(status_code=400, detail="프로젝트 이름 형식 오류")
    try:
        value = validate_domain_settings(
            form.get("cloud_domain", ""),
            form.get("dns_mode", ""),
            form.get("hosted_zone_id"),
        )
        version_text = form.get("version", "")
        expected = int(version_text) if version_text else 0
        deployment(request).store.save_project_settings(
            project,
            {
                "cloud_domain": value.cloud_domain,
                "dns_mode": value.dns_mode,
                "hosted_zone_id": value.hosted_zone_id,
            },
            updated_by="local-operator",
            expected_version=expected,
        )
    except (DdakToolError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/settings?project={project}", status_code=303)
