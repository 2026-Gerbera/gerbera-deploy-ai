from datetime import timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.redact import redact_obj
from ddak.web.dependencies import TERMINAL_STATUSES, deployment, public_links, templates
from ddak.web.form_errors import FormRoute
from ddak.web.forms import parse_form
from ddak.web.security import csrf_token, issue_csrf, require_safe_post
from ddak.web.story import approval_story

router = APIRouter(prefix="/runs", route_class=FormRoute)


@router.get("/{run_id}/approval")
async def approval_page(request: Request, run_id: str):
    try:
        service = deployment(request)
        status = service.get_run(run_id)["status"]
        view = service.approval_view(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행을 찾을 수 없습니다") from exc
    except DdakToolError as exc:
        if exc.code == ErrorCode.PRECONDITION_FAILED:
            return RedirectResponse(f"/runs/{run_id}/result", status_code=303)
        raise
    read_only = status not in {"AWAITING_APPROVAL", "APPROVED"}
    approved_time = None
    if read_only:
        records = service.get_approvals(run_id)
        approved_at = min(
            (record.approved_at for record in records if record.decision == "approved"),
            default=None,
        )
        if approved_at is not None:
            approved_time = approved_at.astimezone(timezone(timedelta(hours=9))).strftime("%H:%M")
    view = redact_obj({**view, "patch": None})  # 원문 대신 검증된 패턴·해시·키만 표시한다.
    token = csrf_token(request)
    reviews = getattr(service, "patch_reviews", None)
    review = reviews.get(run_id) if reviews is not None else None
    # 표시 자료가 없는 서비스(이전 구현·테스트 대역)도 계획만으로 승인 화면을 그린다.
    reader = getattr(service, "get_display_data", None)
    display = reader(run_id) if reader is not None else {}
    response = templates.TemplateResponse(
        request=request,
        name="approval.html",
        context={
            "approval": view,
            "public_links": public_links(service.get_run(run_id).get("context") or {}),
            "story": approval_story(view, display),
            "project": view.get("project"),
            "csrf_token": token,
            "patch_review": review,
            "patch_review_enabled": reviews is not None,
            "start_retry": status == "APPROVED",
            "read_only": read_only,
            "run_status": status,
            "approved_time": approved_time,
            "run_finished": status in TERMINAL_STATUSES,
        },
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
        # 시작 전 검사 실패 후 재시도해도 이미 받은 대상별 승인을 다시 만들지 않는다.
        if not approved or service.get_run(run_id)["status"] != "APPROVED":
            service.approve(run_id, approver="local-operator", approved=approved)
        if approved:
            service.start(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="실행을 찾을 수 없습니다") from None
    page = "progress" if approved else "result"
    return RedirectResponse(f"/runs/{run_id}/{page}", status_code=303)
