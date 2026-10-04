from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from ddak.core.contracts.events import RunEvent
from ddak.core.redact import redact_obj
from ddak.web.dependencies import (
    TERMINAL_STATUSES,
    deployment,
    live_version,
    public_links,
    templates,
)
from ddak.web.i18n import language, translate_tree
from ddak.web.js_wording import wording
from ddak.web.narrative import pipeline_view, track_for
from ddak.web.story import environment_cards, result_story

router = APIRouter(prefix="/runs")
_FINAL = TERMINAL_STATUSES


def _sse(event: dict) -> bytes:
    event_id = int(event["seq"])
    event_type = str(event["type"])
    data = json.dumps(redact_obj(event), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n".encode()


def _event_history(events: list[dict], pipeline: dict, selected: str) -> dict:
    """종료 화면의 기록. SSE와 같은 사전·분류·가림으로 표시 필드만 만든다."""
    words = wording(selected)
    texts = translate_tree(pipeline["texts"]) if selected == "ja" else pipeline["texts"]
    types = {
        "run.state": "event.state",
        "step.started": "event.started",
        "step.finished": "state.succeeded",
        "step.skipped": "state.skipped",
        "gate.waiting": "event.gate_waiting",
        "gate.opened": "event.gate_opened",
        "gate.failed": "event.gate_failed",
        "rollback.started": "event.rollback_started",
        "rollback.finished": "event.rollback_finished",
        "stage.finished": "event.stage_finished",
        "report.ready": "event.report_ready",
        "ai.call": "event.ai_call",
    }
    states = {"running", "waiting", "succeeded", "failed", "check_failed", "skipped", "unrecorded"}
    history = {track: [] for track in ("local", "cloud", "common")}
    for event in redact_obj(events):
        kind = event.get("type", "")
        if kind not in types:
            continue
        track = track_for(event.get("step") or "", event.get("target"))
        step = (
            event.get("preparation_stage")
            if kind == "stage.finished"
            else f"rollback.{track}"
            if kind.startswith("rollback.")
            else event.get("step")
        )
        base = re.sub(r"\.(local|cloud)$", "", step or "")
        key = pipeline["aliases"].get(
            base,
            "build_image"
            if base.startswith("build.")
            else "deploy_tier"
            if base.startswith("deploy.")
            else base,
        )
        text = texts.get(step) or texts.get(event.get("tool")) or texts.get(key) or {}
        try:
            timestamp = (
                datetime.fromisoformat(event["ts"]) if event.get("ts") else datetime.now(UTC)
            )
            time = timestamp.astimezone(timezone(timedelta(hours=9))).strftime("%H:%M:%S")
        except (TypeError, ValueError):
            time = words["time.missing"]
        elapsed = ""
        if event.get("elapsed_s") is not None:
            seconds = max(0, int(event["elapsed_s"]))
            elapsed = (
                words["time.minutes"].format(minutes=seconds // 60, seconds=seconds % 60)
                if seconds >= 60
                else words["time.seconds"].format(seconds=seconds)
            )
        status = event.get("status")
        history[track].append(
            {
                "seq": event["seq"],
                "time": time,
                "name": text.get("name", words["task.name"]),
                "state": words[f"state.{status}" if status in states else types[kind]],
                "elapsed": elapsed,
                "technical": " · ".join(
                    part
                    for part in (step, event.get("detail") if kind.startswith("gate.") else None)
                    if part
                ),
            }
        )
        if kind == "run.state" and status in _FINAL:
            break
    return history


@router.get("/{run_id}")
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
    service = deployment(request)
    snapshot = service.track_result(run_id)
    run = snapshot["run"]
    display = service.get_display_data(run_id)
    view = service.approval_view(run_id) if service.store.prepared(run_id) else {}
    storage = (view.get("infra_summary") or {}).get("storage")
    events = service.events(run_id)
    pipeline = pipeline_view(
        run, display.get("plan") or {}, events, view.get("build_backend"), storage
    )
    previous = next(
        (
            row
            for row in service.list_runs(limit=20, project=run["project"])
            if row["run_id"] != run_id and row["status"] == "SUCCEEDED"
        ),
        None,
    )
    previous_steps = (
        ((service.get_run(previous["run_id"]).get("result") or {}).get("steps") or {})
        if previous
        else {}
    )
    for row in pipeline["rows"]:
        record = previous_steps.get(row["id"], {})
        row["expected_s"] = record.get("elapsed_s") if record.get("status") == "succeeded" else None
    pipeline["remaining"] = sum(row["status"] in {"waiting", "running"} for row in pipeline["rows"])
    pipeline["started"] = next(
        (
            event["ts"]
            for event in events
            if event.get("type") == "run.state" and event.get("status") == "RUNNING"
        ),
        datetime.fromtimestamp(run["created"], UTC).isoformat(),
    )
    links = public_links(run.get("context") or {})
    live_story = result_story(
        run,
        {"images": snapshot["images"]},
        {**display, "events": events, "infra_summary": view.get("infra_summary")},
    )
    cards = environment_cards(run, live_story, links)
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
            "pipeline": pipeline,
            "event_history": _event_history(events, pipeline, language(request))
            if run["status"] in _FINAL
            else {},
            "environment_cards": cards,
            "cards_version": live_version([cards, run.get("result", {}).get("tracks")]),
            "story": live_story,
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
