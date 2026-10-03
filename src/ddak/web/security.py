"""로컬 관리 페이지의 DNS rebinding·cross-site POST 방어."""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, Response

CSRF_COOKIE = "ddak_csrf"


def csrf_token(request: Request) -> str:
    token = request.cookies.get(CSRF_COOKIE)
    if not token or len(token) < 32:
        token = secrets.token_urlsafe(32)
    return token


def issue_csrf(request: Request, response: Response, token: str) -> None:
    if request.cookies.get(CSRF_COOKIE) != token:
        response.set_cookie(
            CSRF_COOKIE,
            token,
            httponly=True,
            secure=False,
            samesite="strict",
            path="/",
        )


def require_safe_post(request: Request, token: str) -> None:
    port = request.app.state.settings.admin_port
    allowed = {f"127.0.0.1:{port}"}
    host = request.headers.get("host", "").lower()
    if host not in allowed:
        raise HTTPException(status_code=400, detail="허용되지 않은 Host")
    origin = request.headers.get("origin")
    if origin not in {f"http://{value}" for value in allowed}:
        raise HTTPException(status_code=403, detail="허용되지 않은 Origin")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not cookie or not token or not secrets.compare_digest(cookie, token):
        raise HTTPException(status_code=403, detail="CSRF 검증 실패")
