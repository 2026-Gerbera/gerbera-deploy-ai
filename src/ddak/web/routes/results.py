from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ddak.core.contracts.errors import DdakToolError
from ddak.core.redact import redact, redact_obj
from ddak.web.dependencies import (
    TERMINAL_STATUSES,
    deployment,
    live_version,
    public_links,
    run_link,
    templates,
)
from ddak.web.form_errors import FormRoute
from ddak.web.story import environment_cards, result_story

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
    if run["status"] not in TERMINAL_STATUSES:
        if run["status"] == "AWAITING_APPROVAL":
            try:
                deployment(request).approval_view(run_id)
            except DdakToolError as exc:
                return templates.TemplateResponse(
                    request=request,
                    name="approval_unavailable.html",
                    status_code=409,
                    context={
                        "project": run["project"],
                        "run_id": run_id,
                        "detail": redact(exc.message),
                    },
                )
        return RedirectResponse(run_link(run), status_code=303)
    summary = deployment(request).reports.get(run_id)
    display = {
        **deployment(request).get_display_data(run_id),
        "events": deployment(request).events(run_id),
    }
    story = result_story(run, deployment(request).get_release(run_id), display)
    return templates.TemplateResponse(
        request=request,
        name="result.html",
        context={
            "run": redact_obj(run),
            "report_summary": summary,
            "summary_version": live_version(summary),
            "story": story,
            "environment_cards": environment_cards(
                run, story, public_links(run.get("context") or {})
            ),
            "project": run["project"],
            "result": redact_obj(run.get("result") or {}),
            "public_links": public_links(run.get("context") or {}),
        },
    )
