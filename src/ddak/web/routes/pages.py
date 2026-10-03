from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

from ddak.web.dependencies import (
    deployment,
    live_version,
    preparation_failure,
    project_runs,
    project_settings,
    public_links,
    run_link,
    selected_project,
    templates,
    watch_warnings,
)
from ddak.web.form_errors import FormRoute
from ddak.web.narrative import planned_rows, preparation_rows, wording
from ddak.web.security import csrf_token, issue_csrf

router = APIRouter(route_class=FormRoute)


def project_summary(service, project):
    runs = project_runs(service, project)
    latest = runs[0] if runs else None
    record = service.get_run(latest["run_id"]) if latest else {}
    context = record.get("context") or {}
    preparations = service.list_preparations(project)
    preparing = any(row["status"] == "PREPARING" for row in preparations)
    failed_preparation = preparation_failure(preparations, runs)
    state = service.project_state(project)
    awaiting = next((row for row in runs if row["status"] == "AWAITING_APPROVAL"), None)
    active = next((row for row in runs if row["status"] in {"APPROVED", "RUNNING"}), None)
    status = latest["status"] if latest else "IDLE"
    url = f"/?project={project}"
    action = "프로젝트 보기"
    if state["blocked_targets"]:
        status, action = "NEEDS_HUMAN", "운영 상태 확인"
        url = f"/ops?project={project}"
    elif preparing:
        status, action = "PREPARING", "준비 상태 보기"
    elif active:
        status, action, url = active["status"], "진행 보기", run_link(active)
    elif awaiting:
        status, action, url = "AWAITING_APPROVAL", "검토하고 승인", run_link(awaiting)
    elif failed_preparation:
        status = failed_preparation["status"]
    elif latest:
        action, url = "결과 보기", run_link(latest)
    settings = service.get_project_settings(project) or {}
    tracks = (record.get("result") or {}).get("tracks") or {}
    targets = context.get("targets")
    return {
        "project": project,
        "status": status,
        "url": url,
        "action": action,
        "sha": (context.get("source_sha") or "")[:7],
        "ref": context.get("ref") or settings.get("watch_branch") or "prod",
        "latest_url": run_link(latest) if latest else None,
        "tracks": {
            name: tracks.get(
                name,
                "N/A"
                if (name == "local" and targets == "cloud")
                or (name == "cloud" and targets in ("local", "onprem"))
                else "UNKNOWN"
                if latest
                else "IDLE",
            )
            for name in ("local", "cloud")
        },
    }


@router.get("/projects")
async def projects_overview(request: Request):
    """전체 프로젝트 목록. 첫 화면 `/`는 기본 프로젝트 대시보드를 연다."""
    service = deployment(request)
    token = csrf_token(request)
    projects = [project_summary(service, name) for name in service.list_projects()]
    response = templates.TemplateResponse(
        request=request,
        name="overview.html",
        context={
            "projects": projects,
            # 저장 설정이 하나도 없으면 다른 화면과 같은 프로젝트 생성 안내를 함께 보인다.
            "project_required": not service.list_project_settings(),
            "project": None,
            "csrf_token": token,
            "watch_warnings": watch_warnings(request),
            "live_version": live_version(projects),
        },
    )
    issue_csrf(request, response, token)
    return response


@router.get("/")
async def dashboard(request: Request, project: str | None = None):
    service = deployment(request)
    token = csrf_token(request)
    project = selected_project(request, project)
    runs = project_runs(service, project)
    links = {}
    for run in runs:
        run["url"] = run_link(run)
        context = service.get_run(run["run_id"]).get("context") or {}
        run.update(
            sha=(context.get("source_sha") or "")[:7] or "기록 없음",
            ref=context.get("ref") or "기록 없음",
            targets=context.get("targets") or "기록 없음",
            trigger={"auto": "자동", "manual": "수동"}.get(context.get("trigger"), "기록 없음"),
            started=datetime.fromtimestamp(run["created"], ZoneInfo("Asia/Seoul")).strftime(
                "%m-%d %H:%M"
            ),
            duration=(run["finished"] - run["created"]) if run.get("finished") else None,
        )
        if run["status"] == "AWAITING_APPROVAL":
            data = service.get_display_data(run["run_id"])
            view = service.approval_view(run["run_id"])
            run["storage"] = (view.get("infra_summary") or {}).get("storage")
            rows = planned_rows(data.get("plan") or {}, storage=run["storage"])
            run["preparation_counts"] = {
                "findings": len(data.get("findings", [])),
                "patches": len(data.get("patches", [])),
                "steps": sum(row["status"] != "skipped" for row in rows),
                "skipped": sum(row["status"] == "skipped" for row in rows),
            }
        for link in public_links(context):
            links.setdefault(link["label"], link)
    preparations = list(reversed(service.list_preparations(project)))
    for item in preparations:
        item["url"] = run_link(item) if item.get("run_id") else None
    state = service.project_state(project)
    settings = project_settings(request, project)
    preparing = service.preparing_run(project)
    preparation_events = service.events(preparing) if preparing else []
    preparing_rows = (
        preparation_rows(
            preparation_events,
            code_patch=settings.get("code_patch", False),
            cloud=settings.get("default_targets") in {"both", "cloud"},
        )
        if preparing
        else []
    )
    awaiting = next((run for run in runs if run["status"] == "AWAITING_APPROVAL"), None)
    storage = None if preparing else (awaiting or {}).get("storage")
    if preparing and service.store.prepared(preparing):
        storage = (service.approval_view(preparing).get("infra_summary") or {}).get("storage")
    if storage and storage.get("intent") in {"create", "remove"}:
        text = wording("deploy.infra.cloud", storage=storage)
        preparing_rows.append(
            {
                "name": text["checklist"],
                "status": "waiting",
                "actor": "사람 승인",
                "elapsed_s": None,
                "sentence": text["description"],
                "warning": text["warning"],
            }
        )
    setup_checklist = (
        service.onboarding.view(project)["checklist"] if service.onboarding is not None else []
    )
    response = templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "project": project,
            "runs": runs,
            "settings": settings,
            "preparing_rows": preparing_rows,
            "state": state,
            "preparations": preparations,
            "preparation_failure": preparation_failure(list(reversed(preparations)), runs),
            "public_links": list(links.values()),
            "watch_warnings": watch_warnings(request),
            "csrf_token": token,
            "connection_attention": any(check["status"] != "green" for check in setup_checklist),
            "live_version": live_version(
                [
                    runs,
                    preparations,
                    state["blocked_targets"],
                    links,
                    setup_checklist,
                    preparation_events,
                ]
            ),
        },
    )
    issue_csrf(request, response, token)
    return response
