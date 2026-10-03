"""관리 폼 오류: fetch 응답 또는 세션에 묶인 일회성 303 알림."""

from __future__ import annotations

import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.routing import APIRoute

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.redact import REDACTED, redact
from ddak.web.security import CSRF_COOKIE, csrf_token, issue_csrf

FLASH_QUERY = "_form_error"
FLASH_TTL = 300
FLASH_LIMIT = 128
_PAGE = re.compile(
    r"/(?:setup(?:/actions)?|settings|ops|(?:ops/)?runs/[A-Za-z0-9_-]+/(?:approval|patch-review))?"
)
_PROJECT = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_FORM = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,127}")


class FormError(HTTPException):
    """기존 라우트의 HTTP 상태를 유지하며 통제된 툴 오류 코드를 전달한다."""

    def __init__(self, error: DdakToolError, status_code: int):
        super().__init__(status_code, redact(error.message))
        self.code = error.code.value


def return_page(raw: str) -> str | None:
    """자체 GET 폼 화면과 공개 project만 허용한다. 오류·비밀값은 URL에 싣지 않는다."""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.fragment or not _PAGE.fullmatch(parsed.path):
        return None
    path = parsed.path.removeprefix("/ops") if parsed.path.startswith("/ops/runs/") else parsed.path
    project = parse_qs(parsed.query).get("project", [""])[-1]
    return path + ("?" + urlencode({"project": project}) if _PROJECT.fullmatch(project) else "")


def form_return_to(request: Request) -> str:
    return return_page(request.url.path + "?" + request.url.query) or _fallback(
        request, getattr(request.state, "submitted_form", {})
    )


def _fallback(request: Request, form: dict[str, str]) -> str:
    path = request.url.path
    if path.startswith("/setup/actions"):
        path = "/setup/actions"
    elif path.startswith("/setup"):
        path = "/setup"
    elif path == "/settings/deploy":
        path = "/"
    elif path.startswith("/ops/"):
        path = "/ops"
    return return_page(path + "?" + urlencode({"project": form.get("project", "")})) or "/"


def error_data(exc: Exception, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
    if isinstance(exc, DdakToolError):
        status, code, message = 409, exc.code.value, exc.message
    elif isinstance(exc, HTTPException):
        status, code = exc.status_code, getattr(exc, "code", f"HTTP_{exc.status_code}")
        message = exc.detail if isinstance(exc.detail, str) else "요청 입력을 확인하세요"
    elif isinstance(exc, ValueError | KeyError):
        status, code, message = 400, ErrorCode.CONFIG_INVALID.value, "설정 입력을 확인하세요"
    else:
        status, code = 502, ErrorCode.INTERNAL.value
        message = "요청 처리에 실패했습니다. 연결 설정과 실행 상태를 확인하세요"
    if code == ErrorCode.PRECONDITION_FAILED.value and message in {
        "설정이 다른 화면에서 변경됐다. 새로고침하세요",
        "같은 설정이 다른 화면에서 변경됐다. 새로고침하세요",
        "설정 기준 버전이 없다. 새로고침하세요",
        "설정 기준 화면이 만료됐다. 새로고침하세요",
    }:
        message = "다른 화면에서 설정이 바뀌었습니다. 새로고침 후 다시 저장하세요"
    for key in ("value", "token"):
        for value in (form.get(key, ""), form.get(key, "").strip()):
            if value:
                message = message.replace(value, REDACTED)
    return status, {"code": code, "message": redact(message)}


def _flashes(request: Request) -> OrderedDict:
    if not hasattr(request.app.state, "form_flashes"):
        request.app.state.form_flashes = OrderedDict()
    cache = request.app.state.form_flashes
    now = time.monotonic()
    for ident in list(cache):
        if cache[ident]["expires"] <= now:
            del cache[ident]
    return cache


def form_context(request: Request) -> dict[str, Any]:
    if not hasattr(request.state, "post_error"):
        request.state.post_error = None
        cache = _flashes(request)
        ident = (
            request.query_params.get(FLASH_QUERY, "") if request.scope.get("query_string") else ""
        )
        record = cache.get(ident)
        if (
            record
            and _page_key(str(request.url)) == _page_key(record["destination"])
            and (secrets.compare_digest(record["csrf"], request.cookies.get(CSRF_COOKIE, "")))
        ):
            # 결과 화면으로 이동한 승인도 오류를 잃지 않도록 실제 렌더에서만 소비한다.
            request.state.post_error = cache.pop(ident)["error"]
    return {"post_error": request.state.post_error}


def form_error_for(request: Request, ident: str | None = None) -> dict | None:
    error = form_context(request)["post_error"]
    if (
        error
        and not getattr(request.state, "form_error_shown", False)
        and (ident is None or error["form_id"] == ident)
    ):
        request.state.form_error_shown = True
        return error
    return None


def _page_key(raw: str) -> tuple[str, str]:
    parsed = urlsplit(raw)
    path = parsed.path.removeprefix("/ops") if parsed.path.startswith("/ops/runs/") else parsed.path
    return path, parse_qs(parsed.query).get("project", [""])[-1]


def failure_response(request: Request, exc: Exception) -> Response:
    form = getattr(request.state, "submitted_form", {})
    status, error = error_data(exc, form)
    if request.headers.get("x-ddak-form") == "1" or "text/html" not in request.headers.get(
        "accept", ""
    ):
        return JSONResponse(
            {"error": error}, status_code=status, headers={"Cache-Control": "no-store"}
        )
    metadata = getattr(request.state, "form_metadata", {})
    destination = return_page(metadata.get("_return_to", "")) or _fallback(request, form)
    ident = metadata.get("_form_id", "")
    error["form_id"] = ident if _FORM.fullmatch(ident) else ""
    # 라우트가 CSRF 확인 후 선별한 입력만 허용된 복귀 화면의 일회성 알림에 보관한다.
    retained = getattr(request.state, "retained_form", None)
    retained_paths = getattr(request.state, "retained_paths", {request.url.path})
    if retained and _page_key(destination)[0] in retained_paths:
        error["submitted"] = retained
    token, flash = csrf_token(request), secrets.token_urlsafe(32)
    cache = _flashes(request)
    while len(cache) >= FLASH_LIMIT:
        cache.popitem(last=False)
    cache[flash] = {
        "csrf": token,
        "error": error,
        "expires": time.monotonic() + FLASH_TTL,
        "destination": destination,
    }
    destination += ("&" if "?" in destination else "?") + urlencode({FLASH_QUERY: flash})
    response = RedirectResponse(destination, status_code=303, headers={"Cache-Control": "no-store"})
    issue_csrf(request, response, token)
    return response


class FormRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                response = await handler(request)
            except Exception as exc:
                if request.method == "POST":
                    return failure_response(request, exc)
                if "text/html" not in request.headers.get("accept", ""):
                    raise
                from ddak.web.dependencies import templates

                status, error = error_data(exc, {})
                original = form_context(request)["post_error"]
                if original:
                    error = original
                    request.state.form_error_shown = True
                response = templates.TemplateResponse(
                    request=request,
                    name="form_failure.html",
                    context={"error": error},
                    status_code=status,
                )
            # 닫힌 승인 화면이 결과 화면으로 이동해도 오류는 해당 세션에 전달한다.
            flash = request.query_params.get(FLASH_QUERY, "")
            record = _flashes(request).get(flash)
            if (
                record
                and response.status_code == 303
                and (
                    secrets.compare_digest(record["csrf"], request.cookies.get(CSRF_COOKIE, ""))
                    and _page_key(str(request.url)) == _page_key(record["destination"])
                )
            ):
                destination = response.headers.get("location", "")
                if re.fullmatch(r"/runs/[A-Za-z0-9_-]+/(?:approval|progress|result)", destination):
                    record["destination"] = destination
                    response.headers["location"] = (
                        destination + "?" + urlencode({FLASH_QUERY: flash})
                    )
            if getattr(request.state, "post_error", None):
                response.headers["Cache-Control"] = "no-store"
            return response

        return handle
