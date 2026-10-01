from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ddak.web.dependencies import deployment

router = APIRouter(prefix="/runs")


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
