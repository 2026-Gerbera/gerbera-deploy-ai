from fastapi import APIRouter, Request

from ddak.web.dependencies import (
    deployment,
    project_settings,
    public_links,
    run_link,
    selected_project,
    templates,
    watch_warnings,
)
from ddak.web.security import csrf_token, issue_csrf

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, project: str | None = None):
    service = deployment(request)
    project = selected_project(request, project)
    runs = [r for r in service.list_runs(limit=200) if r["project"] == project]
    links = {}
    for run in runs:
        run["url"] = run_link(run)
        for link in public_links(service.get_run(run["run_id"]).get("context") or {}):
            links.setdefault(link["label"], link)
    token = csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "project": project,
            "runs": runs,
            "settings": project_settings(request, project),
            "state": service.project_state(project),
            "preparations": service.list_preparations(project),
            "public_links": list(links.values()),
            "watch_warnings": watch_warnings(request),
            "csrf_token": token,
        },
    )
    issue_csrf(request, response, token)
    return response
