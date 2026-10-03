"""공개 설정과 비밀 입력을 초기 설정 coordinator에 전달한다."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ddak.web.dependencies import deployment, selected_project, templates
from ddak.web.form_errors import FormRoute
from ddak.web.forms import parse_form
from ddak.web.security import csrf_token, issue_csrf, require_safe_post

router = APIRouter(prefix="/setup", route_class=FormRoute)


def _coordinator(request: Request) -> Any:
    coordinator = getattr(deployment(request), "onboarding", None)
    if coordinator is None:
        raise HTTPException(503, "초기 설정 서비스가 연결되지 않았습니다")
    return coordinator


async def _call(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(method, *args, **kwargs)


async def _input(request: Request) -> tuple[str, dict[str, str], Any]:
    form = await parse_form(request)
    require_safe_post(request, form.get("csrf_token", ""))
    project = selected_project(request, form.get("project") or None)
    return project, form, _coordinator(request)


def _redirect(project: str) -> RedirectResponse:
    return RedirectResponse("/setup?" + urlencode({"project": project}), status_code=303)


@router.get("")
async def setup_page(request: Request, project: str | None = None):
    project = selected_project(request, project)
    view = await _call(_coordinator(request).view, project)
    token = csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="setup.html",
        context={"project": project, "setup": view, "csrf_token": token},
    )
    response.headers["Cache-Control"] = "no-store"
    issue_csrf(request, response, token)
    return response


@router.post("/choices")
async def save_choices(request: Request):
    project, form, coordinator = await _input(request)
    try:
        version = int(form.get("version") or "0")
    except ValueError:
        raise HTTPException(400, "설정 버전 형식 오류") from None
    data = {
        field: form.get(field, "").strip() or None
        for field in (
            "generation_provider",
            "generation_model",
            "judgment_provider",
            "judgment_model",
            "llm_effort",
            "build_backend",
            "image_repository",
            "buildx_builder",
            "git_author_name",
            "git_author_email",
        )
    }
    for added in ("buildx_builder", "git_author_name", "git_author_email"):
        if added not in form:
            data.pop(added, None)  # 이전 화면의 제출은 새 설정을 지우지 않는다.
    if "ai_timeout_s" in form:
        try:
            data["ai_timeout_s"] = float(form["ai_timeout_s"]) if form["ai_timeout_s"] else None
        except ValueError:
            raise HTTPException(400, "AI 제한 시간은 양수 초 단위로 입력하세요") from None
    await _call(coordinator.save_choices, project, data, expected_version=version)
    return _redirect(project)


@router.post("/key")
async def save_key(request: Request):
    project, form, coordinator = await _input(request)
    await _call(coordinator.save_key, project, form.get("provider_id", ""), form.get("value", ""))
    return _redirect(project)


@router.post("/key-delete")
async def delete_key(request: Request):
    project, form, coordinator = await _input(request)
    await _call(coordinator.delete_key, project, form.get("provider_id", ""))
    return _redirect(project)


@router.post("/test")
async def test_provider(request: Request):
    project, form, coordinator = await _input(request)
    await _call(coordinator.test_provider, project, form.get("provider_id", ""))
    return _redirect(project)


@router.post("/inventory")
async def register_inventory(request: Request):
    project, form, coordinator = await _input(request)
    try:
        data = json.loads(form.get("inventory", ""))
    except (ValueError, RecursionError):
        raise HTTPException(400, "인벤토리는 JSON 객체로 입력하세요") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "인벤토리는 JSON 객체로 입력하세요")
    await _call(coordinator.register_inventory, project, data)
    return _redirect(project)


@router.post("/status")
async def provider_status(request: Request):
    project, form, coordinator = await _input(request)
    await _call(coordinator.check_provider_status, project, form.get("provider_id", ""))
    return _redirect(project)


@router.post("/env")
async def save_env(request: Request):
    project, form, coordinator = await _input(request)
    await _call(coordinator.save_env, project, form.get("key", "").strip(), form.get("value", ""))
    return _redirect(project)


@router.post("/build-apply")
async def apply_build(request: Request):
    project, form, coordinator = await _input(request)
    plan_hash = form.get("plan_hash", "")
    if not plan_hash:
        raise HTTPException(400, "현재 빌드 준비 계획을 확인한 뒤 승인하세요")
    await _call(coordinator.apply_build, project, plan_hash)
    return _redirect(project)


@router.post("/migration")
async def save_migration(request: Request):
    project, form, coordinator = await _input(request)
    await _call(coordinator.save_migration_url, project, form.get("value", ""))
    return _redirect(project)


@router.post("/docker")
async def login_docker(request: Request):
    project, form, coordinator = await _input(request)
    await _call(
        coordinator.login_docker, project, form.get("username", "").strip(), form.get("token", "")
    )
    return _redirect(project)


@router.post("/probe")
async def probe(request: Request):
    project, form, coordinator = await _input(request)
    kind = form.get("kind", "")
    if kind not in {"ai", "inventory", "repository", "build", "docker"}:
        raise HTTPException(400, "지원하지 않는 연결 검사")
    await _call(coordinator.probe, project, kind)
    return _redirect(project)


@router.post("/git-token")
async def save_git_token(request: Request):
    project, form, coordinator = await _input(request)
    await _call(coordinator.save_git_token, project, form.get("value", ""))
    return _redirect(project)


@router.post("/git-token-delete")
async def delete_git_token(request: Request):
    project, _form, coordinator = await _input(request)
    await _call(coordinator.delete_git_token, project)
    return _redirect(project)
