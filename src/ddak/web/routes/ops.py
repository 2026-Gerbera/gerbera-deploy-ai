"""O1 운영 연결면. C3 새 화면은 공개 서비스와 _ops 부분 템플릿을 재사용한다."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ddak.core.contracts.errors import DdakToolError
from ddak.core.redact import redact
from ddak.web.dependencies import (
    deployment,
    live_version,
    project_runs,
    run_link,
    selected_project,
    templates,
    watch_warnings,
)
from ddak.web.forms import parse_form
from ddak.web.routes.approvals import approval_page as canonical_approval_page
from ddak.web.security import csrf_token, issue_csrf, require_safe_post

router = APIRouter(prefix="/ops")


def _render(request: Request, name: str, context: dict):
    token = csrf_token(request)
    response = templates.TemplateResponse(
        request=request, name=name, context={**context, "csrf_token": token}
    )
    issue_csrf(request, response, token)
    return response


@router.get("")
async def page(request: Request, project: str | None = None):
    service = deployment(request)
    project = selected_project(request, project)
    runs = project_runs(service, project)
    preparations = list(reversed(service.list_preparations(project)))
    for run in runs:
        run["url"] = run_link(run)
    for item in preparations:
        item["url"] = run_link(item) if item.get("run_id") else None
    state = service.project_state(project)
    return _render(
        request,
        "ops.html",
        {
            "project": project,
            "settings": service.get_project_settings(project) or {},
            "state": state,
            "live_version": live_version(
                [runs, preparations, state["blocked_targets"], state["active_runs"]]
            ),
            "preparations": preparations,
            "watch_warnings": watch_warnings(request),
            "runs": runs,
        },
    )


@router.get("/runs/{run_id}/approval")
async def approval_page(request: Request, run_id: str):
    return await canonical_approval_page(request, run_id)


@router.get("/state")
async def state(request: Request, project: str | None = None):
    return deployment(request).project_state(selected_project(request, project))


@router.get("/preparations/{request_id}")
async def preparation(request: Request, request_id: str):
    try:
        return deployment(request).get_preparation(request_id)
    except KeyError:
        raise HTTPException(404, "준비 요청을 찾을 수 없습니다") from None


@router.post("/{action}")
async def operate(request: Request, action: str):
    form = await parse_form(request)
    require_safe_post(request, form.get("csrf_token", ""))
    service = deployment(request)
    project = selected_project(request, form.get("project"))
    try:
        if action == "plan":
            result = service.enqueue_deployment(project, ref=form.get("ref") or None)
            if "text/html" in request.headers.get("accept", ""):
                return RedirectResponse(f"/ops?project={project}", status_code=303)
            result["status_url"] = f"/ops/preparations/{result['request_id']}"
            return JSONResponse(result, status_code=202)
        if action == "unlock":
            result = service.unlock_project(
                project,
                actor="local-operator",
                reason=form.get("reason") or "운영자가 상태 확인 후 해제",
            )
            if "text/html" in request.headers.get("accept", ""):
                return RedirectResponse(f"/?project={project}", status_code=303)
            return result
        if action == "settings":
            service.save_project_settings(
                project,
                {
                    "repo_url": form.get("repo_url"),
                    "watch_branch": form.get("watch_branch") or "prod",
                    "default_targets": form.get("default_targets") or "onprem",
                    "auto_detect": form.get("auto_detect") == "on",
                },
                updated_by="local-operator",
                expected_version=int(form.get("version") or "0"),
            )
            return RedirectResponse(f"/ops?project={project}", status_code=303)
        raise HTTPException(404, "지원하지 않는 운영 요청")
    except DdakToolError as exc:
        raise HTTPException(409, redact(exc.message)) from exc
    except (ValueError, KeyError):
        raise HTTPException(400, "운영 입력 형식 오류") from None
