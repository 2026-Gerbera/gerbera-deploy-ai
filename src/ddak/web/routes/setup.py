"""공개 설정과 비밀 입력을 초기 설정 coordinator에 전달한다."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
from collections import OrderedDict
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.redact import redact, redact_obj
from ddak.web.dependencies import deployment, selected_project, templates
from ddak.web.forms import parse_form
from ddak.web.security import csrf_token, issue_csrf, require_safe_post

CHOICE_FIELDS = (
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
    "ai_timeout_s",
)


def _snapshots(request: Request) -> OrderedDict:
    # 공개 GET 화면만 보관한다. 제출한 값·예외 원문은 저장하지 않는다.
    if not hasattr(request.app.state, "setup_views"):
        request.app.state.setup_views = OrderedDict()
    return request.app.state.setup_views


def _empty_view() -> dict:
    return {
        "settings": {"version": 0},
        "setting_sources": {},
        "providers": [],
        "states": {},
        "keys": {},
        "checklist": [],
        "inventory": None,
        "env_keys": [],
        "build_plan": {},
    }


def _public_form(action: str, form: dict[str, str]) -> dict[str, str]:
    allowed = {"choices": CHOICE_FIELDS, "env": ("key",), "docker": ("username",)}
    public = {key: form[key] for key in allowed.get(action, ()) if key in form}
    if action == "choices" and re.fullmatch(r"[0-9]{1,20}", form.get("version", "")):
        public["version"] = form["version"]
    if action == "choices" and re.fullmatch(r"[a-f0-9]{64}", form.get("settings_view", "")):
        public["settings_view"] = form["settings_view"]
    if action == "inventory":
        raw = form.get("inventory", "")
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            if raw.lstrip().startswith("{"):
                # JSON 파싱 실패에도 닫히지 않은 값과 작은따옴표 비밀값은 되돌려주지 않는다.
                raw = re.sub(
                    r"""(["'][A-Za-z0-9_-]*(?:secret|token(?!s)|password|passwd|api_?key|"""
                    r"""access_?key|authorization|credential)[A-Za-z0-9_-]*["']\s*:\s*)"""
                    r"""(["'])(?:\\(?:[\s\S]|$)|(?!\2)[^\\])*(?:\2|$)""",
                    r"\1\2[REDACTED]\2",
                    raw,
                    flags=re.IGNORECASE,
                )
                public["inventory"] = redact(raw, max_len=None)
        else:
            if isinstance(data, dict):
                try:
                    clean = redact_obj(data, max_len=65536)
                    public["inventory"] = (
                        raw if clean == data else json.dumps(clean, ensure_ascii=False, indent=2)
                    )
                except RecursionError:
                    pass
    return public


def _render(
    request: Request,
    project: str,
    view: dict,
    *,
    status_code: int = 200,
    action: str = "",
    target: str = "",
    public: dict | None = None,
    message: str = "",
):
    view = {**_empty_view(), **copy.deepcopy(view)}
    if action == "choices":
        view["settings"].update(public or {})
        if public and public.get("settings_view"):
            view["settings_view"] = public["settings_view"]
    if action in {"key", "key-delete", "test", "status"} and not any(
        p["id"] == target for p in view["providers"]
    ):
        target = target if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", target) else "provider"
        view["providers"].append(
            {
                "id": target,
                "label": target,
                "kind": "api",
                "roles": [],
                "auth": "확인 필요",
                "models": [],
                "default_model": None,
                "key_name": "인증 키",
            }
        )
    token = csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="setup.html",
        status_code=status_code,
        context={
            "project": project,
            "setup": view,
            "csrf_token": token,
            "submitted": public or {},
            "transferred_watchers": transferred_names(request.query_params.getlist("transferred")),
            "feedback": {
                "action": action,
                "target": target,
                "message": message,
                "status": status_code,
            },
        },
    )
    response.headers["Cache-Control"] = "no-store"
    issue_csrf(request, response, token)
    return response


class SetupRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await handler(request)
            except Exception as exc:
                submission = getattr(request.state, "setup_submission", None)
                if submission is None:
                    raise  # CSRF·Origin·Host·프로젝트 검증은 우회하지 않는다.
                project, action, target, public = submission
                status = exc.status_code if isinstance(exc, HTTPException) else 502
                message = (
                    exc.detail
                    if isinstance(exc, HTTPException)
                    else (
                        "연결 또는 설정 처리에 실패했습니다. "
                        "인증·네트워크·대상 상태를 확인하고 다시 시도하세요."
                    )
                )
                view = _snapshots(request).get(project, _empty_view())
                return _render(
                    request,
                    project,
                    view,
                    status_code=status,
                    action=action,
                    target=target,
                    public=public,
                    message=message,
                )

        return handle


router = APIRouter(prefix="/setup", route_class=SetupRoute)


def transferred_names(value: Any) -> list[str]:
    """저장 결과에서 공개 프로젝트 이름만 알림으로 전달한다."""
    if not isinstance(value, list):
        return []
    return [
        name
        for name in value
        if isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name)
    ]


def _coordinator(request: Request) -> Any:
    coordinator = getattr(deployment(request), "onboarding", None)
    if coordinator is None:
        raise HTTPException(
            503, "초기 설정 서비스가 연결되지 않았습니다. 서비스를 확인한 뒤 다시 시도하세요."
        )
    return coordinator


async def _call(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        result = await asyncio.to_thread(method, *args, **kwargs)
    except DdakToolError as exc:
        messages = {
            ErrorCode.CONFIG_INVALID: (
                "설정값 또는 실행 환경을 사용할 수 없습니다. "
                "입력 형식·제공자·모델·로컬 실행 조건을 확인하고 다시 저장하세요."
            ),
            ErrorCode.PRECONDITION_FAILED: (
                "설정 버전이나 승인 조건이 바뀌었습니다. "
                "입력을 보관한 뒤 최신 설정·계획을 다시 불러와 확인하고 재시도하세요."
            ),
            ErrorCode.LOCK_HELD: (
                "다른 작업이 실행 중입니다. 작업이 끝난 뒤 상태를 확인하고 다시 시도하세요."
            ),
            ErrorCode.APPROVAL_REQUIRED: (
                "승인이 필요합니다. 최신 준비 계획을 확인하고 승인한 뒤 다시 시도하세요."
            ),
            ErrorCode.ADAPTER_TIMEOUT: (
                "연결 시간이 초과됐습니다. "
                "네트워크·대상 상태·제한 시간을 확인한 뒤 다시 검사하세요."
            ),
            ErrorCode.AI_UNAVAILABLE: (
                "AI 연결을 사용할 수 없습니다. 인증·모델·네트워크를 확인한 뒤 다시 검사하세요."
            ),
        }
        message = messages.get(
            exc.code,
            "작업 조건을 충족하지 못했습니다. "
            "연결 상태와 최신 설정·승인 조건을 확인한 뒤 다시 시도하세요.",
        )
        if exc.code == ErrorCode.CONFIG_INVALID and exc.message == (
            "cli backend는 로컬 터미널의 loopback 관리 웹에서만 사용한다; make -C harness run 사용"
        ):
            message = (
                "구독 CLI는 이 PC의 로컬 관리 웹에서만 사용할 수 있습니다. "
                "로컬 실행·접속 환경을 확인하거나 API 제공자로 변경한 뒤 다시 저장하세요."
            )
        elif exc.code == ErrorCode.PRECONDITION_FAILED and exc.message in {
            "설정 기준 버전이 없다. 새로고침하세요",
            "설정 기준 화면이 만료됐다. 새로고침하세요",
            "같은 설정이 다른 화면에서 변경됐다. 새로고침하세요",
        }:
            message = (
                "다른 화면에서 설정이 변경되어 저장하지 않았습니다. "
                "현재 입력을 보관하고 최신 설정을 다시 불러와 차이를 확인한 뒤 저장하세요."
            )
        raise HTTPException(409, message) from None
    except (ValueError, KeyError):
        raise HTTPException(
            400, "입력 형식이나 필수값을 확인하고 수정한 뒤 다시 시도하세요."
        ) from None
    except Exception:
        # SDK·프로세스 예외에도 비밀 입력이나 stdout이 포함될 수 있다.
        raise HTTPException(
            502,
            "연결 또는 설정 처리에 실패했습니다. "
            "인증·네트워크·대상 상태를 확인한 뒤 다시 시도하세요.",
        ) from None
    if isinstance(result, dict) and result.get("status") in {"red", "blocked"}:
        raise HTTPException(
            409,
            "연결 검사에서 준비되지 않은 항목이 확인됐습니다. "
            "인증·설정·실행 환경을 수정한 뒤 다시 검사하세요. "
            "최신 검사 결과는 설정을 다시 불러와 확인할 수 있습니다.",
        )
    return result


async def _input(request: Request) -> tuple[str, dict[str, str], Any]:
    form = await parse_form(request)
    require_safe_post(request, form.get("csrf_token", ""))
    project = selected_project(request, form.get("project") or None)
    action = request.url.path.rsplit("/", 1)[-1]
    target = (
        form.get("provider_id", "") if action in {"key", "key-delete", "test", "status"} else ""
    )
    if action == "probe":
        target = form.get("kind", "")
        if target not in {"ai", "inventory", "repository", "build", "docker"}:
            target = "ai"
    request.state.setup_submission = (project, action, target, _public_form(action, form))
    return project, form, _coordinator(request)


def _redirect(project: str, result: Any = None) -> RedirectResponse:
    params: dict[str, Any] = {"project": project}
    names = (
        transferred_names(result.get("transferred_watchers")) if isinstance(result, dict) else []
    )
    if names:
        params["transferred"] = names
    response = RedirectResponse("/setup?" + urlencode(params, doseq=True), status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("")
async def setup_page(request: Request, project: str | None = None):
    project = selected_project(request, project)
    try:
        view = await _call(_coordinator(request).view, project)
    except HTTPException as exc:
        view = copy.deepcopy(_snapshots(request).get(project, _empty_view()))
        view["configuration_notes"] = [
            "설정을 모두 불러오지 못했습니다. 표시된 값·연결 상태는 최신이 아닐 수 있습니다. "
            "입력을 수정하거나 서비스를 확인한 뒤 다시 불러오세요."
        ]
        return _render(request, project, view, status_code=exc.status_code)
    snapshots = _snapshots(request)
    snapshots[project] = copy.deepcopy(view)
    snapshots.move_to_end(project)
    while len(snapshots) > 16:
        snapshots.popitem(last=False)
    return _render(request, project, view)


@router.post("/choices")
async def save_choices(request: Request):
    project, form, coordinator = await _input(request)
    try:
        version = int(form.get("version") or "0")
        if version < 0:
            raise ValueError
    except ValueError:
        raise HTTPException(
            400,
            "설정 버전 형식이 올바르지 않습니다. "
            "입력을 보관하고 최신 설정을 다시 불러온 뒤 재시도하세요.",
        ) from None
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
            raw = form["ai_timeout_s"].strip()
            timeout = float(raw) if raw else None
            if timeout is not None and (not math.isfinite(timeout) or not 0 < timeout <= 300):
                raise ValueError
            data["ai_timeout_s"] = timeout
        except ValueError:
            raise HTTPException(
                400,
                "AI 제한 시간은 0보다 크고 300 이하인 초 단위 숫자로 수정한 뒤 다시 저장하세요.",
            ) from None
    result = await _call(
        coordinator.save_choices,
        project,
        data,
        expected_version=version,
        **({"view_token": form["settings_view"]} if form.get("settings_view") else {}),
    )
    return _redirect(project, result)


@router.post("/key")
async def save_key(request: Request):
    project, form, coordinator = await _input(request)
    result = await _call(
        coordinator.save_key, project, form.get("provider_id", ""), form.get("value", "")
    )
    return _redirect(project, result)


@router.post("/key-delete")
async def delete_key(request: Request):
    project, form, coordinator = await _input(request)
    result = await _call(coordinator.delete_key, project, form.get("provider_id", ""))
    return _redirect(project, result)


@router.post("/test")
async def test_provider(request: Request):
    project, form, coordinator = await _input(request)
    result = await _call(coordinator.test_provider, project, form.get("provider_id", ""))
    return _redirect(project, result)


@router.post("/inventory")
async def register_inventory(request: Request):
    project, form, coordinator = await _input(request)
    try:
        data = json.loads(form.get("inventory", ""))
    except (ValueError, RecursionError):
        raise HTTPException(
            400, "인벤토리는 JSON 객체로 입력하세요. 중괄호·따옴표·쉼표를 수정하고 다시 등록하세요."
        ) from None
    if not isinstance(data, dict):
        raise HTTPException(
            400, "인벤토리는 JSON 객체로 입력하세요. mode와 tiers를 확인하고 다시 등록하세요."
        )
    result = await _call(coordinator.register_inventory, project, data)
    return _redirect(project, result)


@router.post("/status")
async def provider_status(request: Request):
    project, form, coordinator = await _input(request)
    result = await _call(coordinator.check_provider_status, project, form.get("provider_id", ""))
    return _redirect(project, result)


@router.post("/env")
async def save_env(request: Request):
    project, form, coordinator = await _input(request)
    result = await _call(
        coordinator.save_env, project, form.get("key", "").strip(), form.get("value", "")
    )
    return _redirect(project, result)


@router.post("/build-apply")
async def apply_build(request: Request):
    project, form, coordinator = await _input(request)
    plan_hash = form.get("plan_hash", "")
    if not plan_hash:
        raise HTTPException(400, "현재 빌드 준비 계획을 다시 불러와 내용을 확인한 뒤 승인하세요.")
    result = await _call(coordinator.apply_build, project, plan_hash)
    return _redirect(project, result)


@router.post("/migration")
async def save_migration(request: Request):
    project, form, coordinator = await _input(request)
    result = await _call(coordinator.save_migration_url, project, form.get("value", ""))
    return _redirect(project, result)


@router.post("/docker")
async def login_docker(request: Request):
    project, form, coordinator = await _input(request)
    result = await _call(
        coordinator.login_docker, project, form.get("username", "").strip(), form.get("token", "")
    )
    return _redirect(project, result)


@router.post("/probe")
async def probe(request: Request):
    project, form, coordinator = await _input(request)
    kind = form.get("kind", "")
    if kind not in {"ai", "inventory", "repository", "build", "docker"}:
        raise HTTPException(
            400, "지원하지 않는 연결 검사입니다. 화면의 검사 항목을 선택하고 다시 시도하세요."
        )
    result = await _call(coordinator.probe, project, kind)
    return _redirect(project, result)


@router.post("/git-token")
async def save_git_token(request: Request):
    project, form, coordinator = await _input(request)
    result = await _call(coordinator.save_git_token, project, form.get("value", ""))
    return _redirect(project, result)


@router.post("/git-token-delete")
async def delete_git_token(request: Request):
    project, _form, coordinator = await _input(request)
    result = await _call(coordinator.delete_git_token, project)
    return _redirect(project, result)
