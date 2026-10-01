from fastapi import APIRouter, HTTPException, Request

from ddak.web.dependencies import deployment, templates

router = APIRouter(prefix="/runs")


@router.get("/{run_id}/result")
async def result_page(request: Request, run_id: str):
    try:
        run = deployment(request).store.run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행을 찾을 수 없습니다") from exc
    return templates.TemplateResponse(
        request=request,
        name="result.html",
        context={"run": run, "result": run.get("result") or {}},
    )
