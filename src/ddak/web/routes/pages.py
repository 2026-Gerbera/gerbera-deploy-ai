from datetime import datetime
from zoneinfo import ZoneInfo

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
from ddak.web.form_errors import FormRoute
from ddak.web.security import csrf_token, issue_csrf

router = APIRouter(route_class=FormRoute)


@router.get("/")
async def dashboard(request: Request, project: str | None = None):
    service = deployment(request)
    project = selected_project(request, project)
    runs = [r for r in service.list_runs(limit=200) if r["project"] == project]
    links = {}
    for run in runs:
        run["url"] = run_link(run)
        context = service.get_run(run["run_id"]).get("context") or {}
        run.update(
            sha=(context.get("source_sha") or "")[:7] or "기록 없음",
            ref=context.get("ref") or "기록 없음",
            targets=context.get("targets") or "기록 없음",
            trigger={"auto": "자동", "manual": "수동"}.get(context.get("trigger"), "기록 없음"),
            started=datetime.fromtimestamp(run["created"], ZoneInfo("Asia/Seoul")).strftime(
                "%m-%d %H:%M"
            ),
            duration=(run["finished"] - run["created"]) if run.get("finished") else None,
        )
        for link in public_links(context):
            links.setdefault(link["label"], link)
    preparations = list(reversed(service.list_preparations(project)))
    for item in preparations:
        item["url"] = run_link(item) if item.get("run_id") else None
    token = csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "project": project,
            "runs": runs,
            "settings": project_settings(request, project),
            "state": service.project_state(project),
            "preparations": preparations,
            "public_links": list(links.values()),
            "watch_warnings": watch_warnings(request),
            "csrf_token": token,
            "setup_checklist": service.onboarding.view(project)["checklist"]
            if service.onboarding is not None
            else [],
        },
    )
    issue_csrf(request, response, token)
    return response
