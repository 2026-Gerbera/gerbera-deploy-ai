"""smoke_test 공통 로직: HTTP 클라이언트, 시나리오, 판정. 환경 차이는 어댑터가 준다.

- 같은 시나리오를 두 환경에 독립적으로 보낸다(세션·쿠키를 환경 간에 재사용하지 않는다).
- 리다이렉트는 따라가지 않고 상태 코드와 Location 경로를 그대로 본다.
- 검사 불합격은 passed=False로 돌려준다. 주소가 없는 등 수행할 수 없으면 DdakToolError.
"""

from __future__ import annotations

import http.client
import json
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, Literal, Protocol
from urllib.parse import urlencode, urlsplit

from ddak.core.adapters import TargetAdapter
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.smoke_test import (
    Observed,
    SmokeScenario,
    SmokeTestInput,
    SmokeTestOutput,
)

MAX_BODY = 256 * 1024
REQUEST_TIMEOUT_S = 10.0
SESSION_COOKIE = "session"


@dataclass(frozen=True)
class Response:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: str

    def header(self, name: str) -> str | None:
        values = self.all(name)
        return values[0] if values else None

    def all(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k.lower() == name.lower()]


class HttpClient(Protocol):
    def request(
        self, method: str, path: str, form: Mapping[str, str] | None, timeout: float
    ) -> Response: ...


class SmokeAdapter(TargetAdapter, Protocol):
    @property
    def name(self) -> str: ...

    @property
    def source(self) -> Literal["live", "fixture"]: ...

    def client(self, ctx: RunContext) -> HttpClient: ...


class UrlClient:
    """표준 라이브러리 HTTP 클라이언트. https는 인증서·호스트명을 검증한다(끄지 않는다)."""

    def __init__(self, base_url: str) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "스모크 대상 주소 형식 오류")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "스모크 대상 주소 형식 오류")
        self._https = parts.scheme == "https"
        self._host = parts.hostname
        self._port = parts.port
        self._prefix = parts.path.rstrip("/")

    def request(
        self, method: str, path: str, form: Mapping[str, str] | None, timeout: float
    ) -> Response:
        conn: http.client.HTTPConnection
        if self._https:
            conn = http.client.HTTPSConnection(
                self._host, self._port, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=timeout)
        headers = {"User-Agent": "ddak-smoke/1", "Accept": "*/*"}
        body = None
        if form is not None:
            body = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            conn.request(method, self._prefix + path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read(MAX_BODY + 1)[:MAX_BODY]
            return Response(resp.status, tuple(resp.getheaders()), data.decode("utf-8", "replace"))
        finally:
            conn.close()


# ---- 시나리오 -----------------------------------------------------------------


@dataclass
class Probe:
    """한 번의 smoke 실행 상태. 시나리오 함수가 공유한다."""

    client: HttpClient
    ctx: RunContext
    title: str  # 이번 run이 만든 글 제목(표시: [smoke <run_id>])

    def timeout(self) -> float:
        if self.ctx.deadline is None:
            return REQUEST_TIMEOUT_S
        remaining = self.ctx.deadline - time.monotonic()
        if remaining <= 0:
            raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "스모크 제한 시간 초과")
        return min(REQUEST_TIMEOUT_S, remaining)

    def get(self, path: str) -> Response:
        return self.client.request("GET", path, None, self.timeout())

    def post(self, path: str, form: Mapping[str, str]) -> Response:
        return self.client.request("POST", path, form, self.timeout())


Check = Callable[[Probe], SmokeScenario]


def _json(resp: Response) -> dict[str, Any]:
    value = json.loads(resp.body)
    if not isinstance(value, dict):
        raise ValueError("JSON 객체가 아니다")
    return value


def _location_path(resp: Response) -> str | None:
    location = resp.header("Location")
    if location is None:
        return None
    return urlsplit(location).path or "/"


def cookie_attrs(resp: Response) -> dict[str, Observed]:
    """Set-Cookie에서 세션 쿠키의 이름·속성만 뽑는다. 값은 버린다."""
    for raw in resp.all("Set-Cookie"):
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:  # 형식이 깨진 헤더는 이름만 본다
            if raw.split("=", 1)[0].strip() == SESSION_COOKIE:
                return {"cookie": SESSION_COOKIE}
            continue
        morsel = jar.get(SESSION_COOKIE)
        if morsel is not None:
            return {
                "cookie": SESSION_COOKIE,
                "httponly": bool(morsel["httponly"]),
                "secure": bool(morsel["secure"]),
                "samesite": str(morsel["samesite"] or ""),
                "path": str(morsel["path"] or ""),
            }
    return {"cookie": None}


def _scalar(value: object) -> Observed:
    """앱 응답은 신뢰하지 않는 입력이다. 짧은 스칼라로만 남긴다."""
    if value is None or isinstance(value, bool | int):
        return value
    return str(value)[:128]


def _scenario(
    sid: str, ok: bool, resp: Response | None, detail: str, normalized: Mapping[str, object]
) -> SmokeScenario:
    return SmokeScenario(
        id=sid,
        ok=ok,
        status=resp.status if resp is not None and 100 <= resp.status <= 599 else None,
        detail="" if ok else detail[:200],
        normalized={key: _scalar(value) for key, value in normalized.items()},
    )


def s0_version(p: Probe) -> SmokeScenario:
    resp = p.get("/version")
    data = _json(resp) if resp.status == 200 else {}
    db = data.get("db") if isinstance(data.get("db"), dict) else {}
    normalized: dict[str, Observed] = {
        "release_id": data.get("release_id"),
        "schema_expected": data.get("schema_expected"),
        "app_env": data.get("app_env"),
        "db.dialect": db.get("dialect"),
        "db.tls": db.get("tls"),
        "db.tls_verified": db.get("tls_verified"),
    }
    problems = []
    if resp.status != 200:
        problems.append(f"/version 상태 {resp.status}")
    if data.get("release_id") != p.ctx.run_id:
        problems.append("release_id가 이번 run과 다름")
    if db.get("dialect") != "mysql":
        problems.append("DB가 mysql이 아님")
    return _scenario("S0.version", not problems, resp, ", ".join(problems), normalized)


def s0_ready(p: Probe) -> SmokeScenario:
    resp = p.get("/health/ready")
    data = _json(resp) if resp.status in (200, 503) else {}
    schema = data.get("schema") if isinstance(data.get("schema"), dict) else {}
    normalized: dict[str, Observed] = {
        "status": data.get("status"),
        "schema.current": schema.get("current"),
        "schema.expected": schema.get("expected"),
    }
    ok = resp.status == 200 and data.get("status") == "ok"
    return _scenario("S0.ready", ok, resp, f"/health/ready 상태 {resp.status}", normalized)


def b1_index(p: Probe) -> SmokeScenario:
    resp = p.get("/")
    cookie = cookie_attrs(resp)
    ok = resp.status == 200 and "Posts" in resp.body
    return _scenario("B1", ok, resp, f"목록 상태 {resp.status}", {"status": resp.status, **cookie})


def b2_create(p: Probe) -> SmokeScenario:
    resp = p.post("/create", {"title": p.title, "body": "smoke"})
    location = _location_path(resp)
    listed = p.get("/") if resp.status == 302 else None
    shown = listed is not None and listed.status == 200 and p.title in listed.body
    ok = resp.status == 302 and location == "/" and shown
    detail = f"글쓰기 상태 {resp.status}, 이동 {location}, 목록 반영 {shown}"
    return _scenario(
        "B2.create",
        ok,
        resp,
        detail,
        {"status": resp.status, "location": location, "listed": shown, **cookie_attrs(resp)},
    )


def b2_empty_title(p: Probe) -> SmokeScenario:
    resp = p.post("/create", {"title": "", "body": "smoke"})
    shown = "Title is required." in resp.body
    ok = resp.status == 200 and shown
    return _scenario(
        "B2.empty",
        ok,
        resp,
        f"빈 제목 상태 {resp.status}, 오류 문구 {shown}",
        {"status": resp.status, "error_shown": shown},
    )


# 시나리오 묶음. plan step params의 scenarios가 이 이름을 고른다. (id, 검사) 순서대로 실행한다.
# base = v1 익명 게시판(B2는 익명 글쓰기가 되는 동안만 유효).
# v2 기능의 묶음은 기능이 정해지면 추가한다.
GROUPS: dict[str, tuple[tuple[str, Check], ...]] = {
    "base": (
        ("S0.version", s0_version),
        ("S0.ready", s0_ready),
        ("B1", b1_index),
        ("B2.create", b2_create),
        ("B2.empty", b2_empty_title),
    ),
}


def _run_check(sid: str, check: Check, probe: Probe) -> SmokeScenario:
    try:
        return check(probe)
    except DdakToolError:
        raise
    except (OSError, http.client.HTTPException) as error:
        return _scenario(sid, False, None, f"연결 실패: {type(error).__name__}", {})
    except ValueError as error:  # JSON 형식 오류 등
        return _scenario(sid, False, None, f"응답 형식 오류: {type(error).__name__}", {})


def run_smoke(adapter: SmokeAdapter, inp: SmokeTestInput, ctx: RunContext) -> SmokeTestOutput:
    unknown = [g for g in inp.scenarios if g not in GROUPS]
    if unknown:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, f"알 수 없는 스모크 시나리오 묶음: {unknown}")
    started = time.monotonic()
    probe = Probe(client=adapter.client(ctx), ctx=ctx, title=f"[smoke {ctx.run_id}]")
    checks = dict(item for g in inp.scenarios for item in GROUPS[g])  # 같은 검사는 한 번만
    results = [_run_check(sid, check, probe) for sid, check in checks.items()]
    return SmokeTestOutput(
        run_id=inp.run_id,
        target=inp.target,
        passed=bool(results) and all(r.ok for r in results),
        elapsed_s=round(time.monotonic() - started, 3),
        scenarios=results,
        source=adapter.source,
    )
