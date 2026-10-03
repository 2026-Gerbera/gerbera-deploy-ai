from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ddak.core.redact import redact_obj
from ddak.web.dependencies import deployment, public_links, templates
from ddak.web.form_errors import FormRoute

router = APIRouter(prefix="/runs", route_class=FormRoute)


@router.get("/{run_id}/result")
async def result_page(request: Request, run_id: str):
    try:
        run = deployment(request).get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행을 찾을 수 없습니다") from exc
    if (run.get("result") or {}).get("setup_operation"):
        return RedirectResponse(
            "/setup/actions?" + urlencode({"project": run["project"]}), status_code=303
        )
    return templates.TemplateResponse(
        request=request,
        name="result.html",
        context={
            "run": redact_obj(run),
            "project": run["project"],
            "result": redact_obj(run.get("result") or {}),
            "public_links": public_links(run.get("context") or {}),
        },
    )
