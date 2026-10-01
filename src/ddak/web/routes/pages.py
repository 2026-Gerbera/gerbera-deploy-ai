from fastapi import APIRouter, Request

from ddak.web.dependencies import deployment, templates
from ddak.web.security import csrf_token, issue_csrf

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, project: str = "flaskr"):
    service = deployment(request)
    token = csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "project": project,
            "runs": service.store.list_runs(),
            "settings": service.store.project_settings(project),
            "csrf_token": token,
        },
    )
    issue_csrf(request, response, token)
    return response
