from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ddak.core.contracts.errors import DdakToolError
from ddak.web.dependencies import deployment, templates
from ddak.web.forms import parse_form
from ddak.web.security import csrf_token, issue_csrf, require_safe_post

router = APIRouter(prefix="/runs")


@router.get("/{run_id}/approval")
async def approval_page(request: Request, run_id: str):
    try:
        view = deployment(request).approval_view(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행을 찾을 수 없습니다") from exc
    except DdakToolError:
        return RedirectResponse(f"/runs/{run_id}/result", status_code=303)
    token = csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="approval.html",
        context={"approval": view, "csrf_token": token},
    )
    issue_csrf(request, response, token)
    return response


@router.post("/{run_id}/approval")
async def decide_approval(request: Request, run_id: str):
    form = await parse_form(request)
    require_safe_post(request, form.get("csrf_token", ""))
    approved = form.get("decision") == "approved"
    if form.get("decision") not in {"approved", "denied"}:
        raise HTTPException(status_code=400, detail="승인 결정 형식 오류")
    try:
        service = deployment(request)
        service.approve(run_id, approver="local-operator", approved=approved)
        if approved:
            service.start(run_id)
    except (KeyError, DdakToolError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/runs/{run_id}/progress", status_code=303)
