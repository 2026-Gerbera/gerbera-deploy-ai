import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.redact import redact
from ddak.web.dependencies import deployment, templates
from ddak.web.form_errors import FormRoute, form_context
from ddak.web.forms import parse_form
from ddak.web.security import csrf_token, issue_csrf, require_safe_post

router = APIRouter(prefix="/runs", route_class=FormRoute)


def _reviews(request: Request):
    reviews = getattr(deployment(request), "patch_reviews", None)
    if reviews is None:
        raise HTTPException(503, "코드 제안 기능이 연결되지 않았습니다")
    return reviews


def _review_response(
    request: Request,
    run_id: str,
    *,
    error: str | None = None,
    status: int = 200,
    submitted: dict | None = None,
):
    service = deployment(request)
    review = _reviews(request).get(run_id)
    if review and review.get("successor"):
        return RedirectResponse(f"/runs/{review['successor']}/approval", status_code=303)
    if service.get_run(run_id)["status"] != "AWAITING_APPROVAL":
        return RedirectResponse(f"/runs/{run_id}/result", status_code=303)
    if (
        review
        and review["state"] == "sealed"
        and request.query_params.get("wait_for_approval") == "1"
    ):
        return RedirectResponse(f"/runs/{run_id}/approval", status_code=303)
    blocked = False
    try:
        review = _reviews(request).view(run_id)
    except DdakToolError as exc:
        blocked, status, error = True, 409, redact(exc.message)
    post_error = form_context(request)["post_error"]
    if post_error and post_error["form_id"] == "patch-review":
        submitted = post_error.get("submitted", {})
    if submitted and (not review or submitted.get("revision") != str(review["revision"])):
        submitted = {}
    if post_error and submitted and review and review["state"] == "ready" and not blocked:
        post_error["inputs_restored"] = True
    selection = list(review.get("selected", [])) if review else []
    if review and submitted and submitted.get("revision") == str(review["revision"]):
        selection = [
            item["id"]
            for item in review["proposals"]
            if item.get("required", False) or submitted.get("apply_" + item["id"]) == "on"
        ]
    elif review:
        selection = [
            item["id"]
            for item in review["proposals"]
            if item.get("required", False) or item["id"] in selection
        ]
    token = csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="patch_review.html",
        status_code=status,
        context={
            "project": service.get_run(run_id)["project"],
            "run_id": run_id,
            "review": review,
            "csrf_token": token,
            "error": error,
            "submitted": submitted or {},
            "blocked": blocked,
            "selection": selection,
        },
    )
    issue_csrf(request, response, token)
    return response


@router.get("/{run_id}/patch-review")
async def patch_review_page(request: Request, run_id: str):
    try:
        return _review_response(request, run_id)
    except KeyError as exc:
        raise HTTPException(404, "실행을 찾을 수 없습니다") from exc


@router.post("/{run_id}/patch-review")
async def patch_review_action(request: Request, run_id: str):
    form = await parse_form(request)
    require_safe_post(request, form.get("csrf_token", ""))
    reviews = _reviews(request)
    try:
        # 새 생성이 목록을 비운 때에도 이전 탭의 초안을 복구한다.
        # 적용 가능 여부는 아래 reviews.request가 현재 제안·revision으로 검사한다.
        request.state.retained_form = {
            key: redact(value[:2000], max_len=None)
            for key, value in form.items()
            if key == "revision" or re.fullmatch(r"(?:apply|prompt)_[a-z0-9_-]{1,64}", key)
        }
        action = form.get("action", "")
        if action == "begin":
            reviews.begin(run_id)
        else:
            if not form.get("revision", "").isascii() or not form.get("revision", "").isdigit():
                raise DdakToolError(ErrorCode.CONFIG_INVALID, "검토 버전 형식이 잘못됐습니다")
            selected = [
                key.removeprefix("apply_")
                for key, value in form.items()
                if key.startswith("apply_") and value == "on"
            ]
            proposal_id = action.removeprefix("revise:")
            reviews.request(
                run_id,
                int(form["revision"]),
                action,
                selected,
                prompt=form.get("prompt_" + proposal_id, ""),
                candidate_id=form.get("candidate_id", ""),
                notes={
                    key.removeprefix("prompt_"): value
                    for key, value in form.items()
                    if key.startswith("prompt_")
                },
            )
            if action == "cancel":
                return RedirectResponse(f"/runs/{run_id}/approval", status_code=303)
    except KeyError as exc:
        raise HTTPException(404, "실행을 찾을 수 없습니다") from exc
    return RedirectResponse(f"/runs/{run_id}/patch-review", status_code=303)


@router.get("/{run_id}/approved-patch.diff")
async def approved_patch(request: Request, run_id: str) -> Response:
    service = deployment(request)
    try:
        view = service.approval_view(run_id)
        approvals = service.store.approvals(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행을 찾을 수 없습니다") from exc
    approved = any(record.kind == "patch" and record.decision == "approved" for record in approvals)
    if not approved or not view.get("patch"):
        raise HTTPException(status_code=404, detail="승인된 패치가 없습니다")
    return Response(
        content=view["patch"],
        media_type="text/x-diff",
        headers={
            "Content-Disposition": 'attachment; filename="approved-patch.diff"',
            "X-Content-Type-Options": "nosniff",
        },
    )
