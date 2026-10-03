"""관리자 DB 준비/소유권 이전. GET은 공개 요약만, 변경은 CSRF 보호 POST만."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ddak.core.contracts.errors import DdakToolError
from ddak.core.redact import redact
from ddak.web.dependencies import deployment, selected_project, templates
from ddak.web.forms import parse_form
from ddak.web.security import csrf_token, issue_csrf, require_safe_post

router = APIRouter(prefix="/setup/actions")


def _actions(request: Request) -> Any:
    coordinator = getattr(deployment(request), "setup_actions", None)
    if coordinator is None:
        raise HTTPException(503, "관리자 준비 작업 서비스가 연결되지 않았습니다")
    return coordinator


async def _page(request: Request, project: str, *, error: str = "", status: int = 200):
    try:
        view = await asyncio.to_thread(_actions(request).view, project)
    except Exception:
        raise HTTPException(502, "준비 작업 요약을 읽을 수 없습니다") from None
    token = csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="setup_actions.html",
        context={
            "project": project,
            "actions": view["actions"],
            "error": error,
            "csrf_token": token,
        },
        status_code=status,
    )
    response.headers["Cache-Control"] = "no-store"
    issue_csrf(request, response, token)
    return response


@router.get("")
async def actions_page(request: Request, project: str | None = None):
    return await _page(request, selected_project(request, project))


async def _input(request: Request):
    form = await parse_form(request)
    require_safe_post(request, form.get("csrf_token", ""))
    project = selected_project(request, form.get("project") or None)
    return project, form, _actions(request)


@router.post("/plan")
async def plan_action(request: Request):
    project, form, actions = await _input(request)
    kind = form.get("kind", "")
    allowed = {"project", "csrf_token", "kind"}
    if kind == "database":
        keys = {"database", "backup_database", "app_account", "migrator_account"}
        arguments = {key: form.get(key, "").strip() for key in keys}
        allowed |= keys
    elif kind == "ownership":
        try:
            arguments = {"tier": form.get("tier", ""), "replica": int(form.get("replica", "1"))}
        except ValueError:
            raise HTTPException(400, "replica는 정수로 입력하세요") from None
        allowed |= {"tier", "replica"}
    else:
        raise HTTPException(400, "지원하지 않는 준비 종류")
    if set(form) - allowed:
        raise HTTPException(400, "준비 입력에 암호값이나 추가 필드를 허용하지 않습니다")
    try:
        await asyncio.to_thread(actions.plan, project, kind, arguments)
    except DdakToolError as exc:
        return await _page(request, project, error=redact(str(exc), max_len=500), status=409)
    except Exception:
        return await _page(
            request, project, error="준비 계획 검사 실패. 연결·소유권을 확인하세요", status=502
        )
    return await _page(request, project)


@router.post("/apply")
async def approve_action(request: Request):
    project, form, actions = await _input(request)
    if set(form) != {"project", "csrf_token", "id", "approved_hash"}:
        raise HTTPException(400, "승인 입력 형식 오류")
    try:
        # 승인자 identity는 HTTP 필드로 받지 않는다. 기존 로컬 관리자 경계와 같다.
        await asyncio.to_thread(
            actions.apply, project, form["id"], form["approved_hash"], "local-operator"
        )
    except DdakToolError as exc:
        return await _page(request, project, error=redact(str(exc), max_len=500), status=409)
    except Exception:
        return await _page(
            request, project, error="준비 실행 실패. 사람이 환경 상태를 확인하세요", status=502
        )
    return await _page(request, project)
