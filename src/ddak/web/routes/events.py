from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from ddak.core.contracts.events import RunEvent
from ddak.core.redact import redact_obj
from ddak.web.dependencies import TERMINAL_STATUSES, deployment, templates

router = APIRouter(prefix="/runs")
_FINAL = TERMINAL_STATUSES


def _sse(event: dict) -> bytes:
    event_id = int(event["seq"])
    event_type = str(event["type"])
    data = json.dumps(redact_obj(event), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n".encode()


@router.get("/{run_id}/progress")
async def progress_page(request: Request, run_id: str):
    try:
        run = deployment(request).get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행을 찾을 수 없습니다") from exc
    if run["status"] == "AWAITING_APPROVAL":
        return RedirectResponse(f"/runs/{run_id}/approval", status_code=303)
    approved = any(
        item.decision == "approved" for item in deployment(request).get_approvals(run_id)
    )
    return templates.TemplateResponse(
        request=request,
        name="progress.html",
        context={
            "run_id": run_id,
            "project": run["project"],
            "status": run["status"],
            "targets": (run.get("context") or {}).get("targets"),
            "result": run.get("result") or {},
            "approved": approved,
            "terminal_states": sorted(_FINAL),
        },
    )


@router.get("/{run_id}/events")
async def run_events(request: Request, run_id: str) -> StreamingResponse:
    service = deployment(request)
    try:
        service.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행을 찾을 수 없습니다") from exc
    header = request.headers.get("last-event-id", "-1")
    try:
        after = int(header)
    except ValueError:
        after = -1

    async def stream() -> AsyncIterator[bytes]:
        queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=128)

        async def receive(event: RunEvent) -> None:
            if queue.full():
                _ = queue.get_nowait()
            queue.put_nowait(event)

        try:
            replay = service.events(run_id, after=after)
            for event in replay:
                yield _sse(event)
                if event.get("type") == "run.state" and event.get("status") in _FINAL:
                    return
            row = service.get_run(run_id)
            if row["status"] in _FINAL:
                yield _sse(
                    {
                        "seq": max((e["seq"] for e in replay), default=after) + 1,
                        "type": "run.state",
                        "run_id": run_id,
                        "status": row["status"],
                    }
                )
                return
            unsubscribe = service.subscribe(run_id, receive)
        except KeyError:
            return
        try:
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield b": keep-alive\n\n"
                    continue
                payload = event.model_dump(mode="json")
                yield _sse(payload)
                if event.type.value == "run.state" and event.status in _FINAL:
                    return
        finally:
            unsubscribe()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
