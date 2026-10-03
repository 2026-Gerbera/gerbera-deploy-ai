"""smoke_test: 실제 레지스트리 + Fake 어댑터, 그리고 로컬 HTTP 서버로 local 어댑터를 확인한다."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from pydantic import ValidationError

from ddak.app import load_tools
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Layer, Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Plan, PlanStep
from ddak.core.contracts.tools.smoke_test import SmokeTestInput, SmokeTestOutput
from ddak.executor.engine import Executor, RunStatus
from ddak.verify.smoke import smoke_test
from ddak.verify.smoke.fake import FakeFlaskr, FakeSmokeAdapter
from ddak.verify.smoke.logic import Response, run_smoke

RUN = "run-smoke-1"
BASE_IDS = ["S0.version", "S0.ready", "B1", "B2.create", "B2.empty", "B2.long"]
# 결과에 나오면 안 되는 값. 비밀값 스캐너에 걸리지 않게 이어 붙여 만든다(AGENTS 4절)
COOKIE_VALUE = "not-a-real-" + "cookie-value"


def test_smoke_test_is_registered_from_its_directory() -> None:
    tool = load_tools().get("smoke_test")
    assert tool.input_model is SmokeTestInput
    assert tool.output_model is SmokeTestOutput


@pytest.mark.parametrize("target", [Target.LOCAL, Target.CLOUD])
def test_fake_adapter_passes_base_scenarios(target: Target) -> None:
    tool = load_tools().get("smoke_test")
    out = tool.fn(SmokeTestInput(run_id=RUN, target=target), RunContext(run_id=RUN))
    assert out.passed is True
    assert out.source == "fixture"
    assert [s.id for s in out.scenarios] == BASE_IDS
    assert out.scenarios[0].normalized["release_id"] == RUN


def test_wrong_release_is_check_failure_not_exception() -> None:
    class OldRelease(FakeFlaskr):
        def request(
            self, method: str, path: str, form: Mapping[str, str] | None, timeout: float
        ) -> Response:
            resp = super().request(method, path, form, timeout)
            if path == "/version":
                return Response(200, resp.headers, resp.body.replace(RUN, "rel-old"))
            return resp

    class Adapter(FakeSmokeAdapter):
        def client(self, ctx: RunContext) -> OldRelease:
            return OldRelease(ctx.run_id)

    out = run_smoke(
        Adapter(Target.LOCAL), SmokeTestInput(run_id=RUN, target="local"), RunContext(RUN)
    )
    assert out.passed is False
    version = out.scenarios[0]
    assert version.id == "S0.version" and version.ok is False
    assert "release_id" in version.detail


def test_untrusted_response_values_do_not_crash_the_tool() -> None:
    class Hostile(FakeFlaskr):
        def request(
            self, method: str, path: str, form: Mapping[str, str] | None, timeout: float
        ) -> Response:
            if path == "/version":
                body = {"release_id": {"nested": True}, "app_env": "x" * 5000, "db": []}
                return Response(200, (), json.dumps(body))
            if path == "/health/ready":
                return Response(200, (), "not json")
            return super().request(method, path, form, timeout)

    class Adapter(FakeSmokeAdapter):
        def client(self, ctx: RunContext) -> Hostile:
            return Hostile(ctx.run_id)

    out = run_smoke(
        Adapter(Target.LOCAL), SmokeTestInput(run_id=RUN, target="local"), RunContext(RUN)
    )
    assert out.passed is False
    version, ready = out.scenarios[0], out.scenarios[1]
    assert version.ok is False and len(str(version.normalized["app_env"])) <= 128
    assert ready.ok is False and ready.detail.startswith("응답 형식 오류")


@pytest.mark.anyio
async def test_smoke_opens_local_verified_through_executor() -> None:
    def step(sid: str, **kw: object) -> PlanStep:
        return PlanStep(id=sid, tool="smoke_test", layer=Layer.MANDATORY, **kw)  # type: ignore[arg-type]

    plan = Plan.model_validate(
        {
            "run_id": RUN,
            "deploy": {
                "local": {
                    "steps": [
                        step(
                            "verify.smoke.local",
                            signal="local_verified",
                            params={"scenarios": ["base"]},
                        )
                    ]
                },
                "cloud": {"steps": [step("verify.smoke.cloud", signal="cloud_verified")]},
            },
        }
    )
    result = await Executor(load_tools()).run(plan, RunContext(run_id=RUN))
    assert result.status is RunStatus.SUCCEEDED
    outputs = [r.output for r in result.records if r.output]
    # 두 트랙은 서로 기다리지 않으므로 끝나는 순서는 정해져 있지 않다
    assert sorted(o["target"] for o in outputs) == ["cloud", "local"]
    assert all(o["passed"] is True for o in outputs)


def test_unknown_scenario_group_is_config_error() -> None:
    with pytest.raises(DdakToolError) as caught:
        smoke_test(SmokeTestInput(run_id=RUN, target="local", scenarios=["auth"]), RunContext(RUN))
    assert caught.value.code is ErrorCode.CONFIG_INVALID


def test_input_rejects_url_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SmokeTestInput.model_validate(
            {"run_id": RUN, "target": "local", "url": "http://evil.example"}
        )
    with pytest.raises(ValidationError):
        SmokeTestInput.model_validate({"run_id": RUN, "target": "local", "scenarios": ["../x"]})


# ---- local 어댑터: 127.0.0.1의 작은 HTTP 서버를 v1 앱처럼 띄운다 -------------------


class _App(BaseHTTPRequestHandler):
    titles: ClassVar[list[str]] = []
    set_cookie: ClassVar[bool] = False

    def log_message(self, *args: object) -> None:  # 테스트 출력 정리
        del args

    def _send(self, status: int, body: str, headers: dict[str, str] | None = None) -> None:
        data = body.encode()
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if self.set_cookie:
            self.send_header(
                "Set-Cookie", f"session={COOKIE_VALUE}; HttpOnly; Path=/; SameSite=Lax"
            )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/version":
            version = {
                "release_id": RUN,
                "schema_expected": "0001",
                "app_env": "onprem",
                "db": {"dialect": "mysql", "tls": True, "tls_verified": False},
            }
            self._send(200, json.dumps(version))
        elif self.path == "/health/ready":
            self._send(200, '{"status": "ok", "schema": {"current": "0001", "expected": "0001"}}')
        elif self.path == "/":
            self._send(200, "<h1>Posts</h1>" + "".join(self.titles))
        else:
            self._send(404, "")

    def do_POST(self) -> None:
        from urllib.parse import parse_qs

        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode())
        title = (form.get("title") or [""])[0]
        if not title:
            self._send(200, "<div>Title is required.</div>")
            return
        if len(title) > 200:
            self._send(200, "<div>Title is too long.</div>")
            return
        type(self).titles.append(title)
        self._send(302, "", {"Location": "http://127.0.0.1/"})


@pytest.fixture
def app_url() -> Iterator[str]:
    _App.titles = []
    _App.set_cookie = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _App)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _local_ctx(url: str | None, **kw: object) -> RunContext:
    platform = {"onprem": {"public_url": url}} if url else {}
    return RunContext(run_id=RUN, adapter_mode=AdapterMode.REAL, platform=platform, **kw)  # type: ignore[arg-type]


def test_local_adapter_runs_against_real_http(app_url: str) -> None:
    out = smoke_test(SmokeTestInput(run_id=RUN, target="local"), _local_ctx(app_url))
    assert out.passed is True, out
    assert out.source == "live"
    create = next(s for s in out.scenarios if s.id == "B2.create")
    assert create.normalized["location"] == "/"  # 호스트를 지우고 경로만 남긴다
    assert create.normalized["cookie"] is None  # v1은 세션 쿠키가 없다
    assert f"[smoke {RUN}]" in _App.titles


def test_cookie_value_never_leaves_the_tool(app_url: str) -> None:
    _App.set_cookie = True
    out = smoke_test(SmokeTestInput(run_id=RUN, target="local"), _local_ctx(app_url))
    b1 = next(s for s in out.scenarios if s.id == "B1")
    assert b1.normalized["cookie"] == "session"
    assert b1.normalized["httponly"] is True and b1.normalized["samesite"] == "Lax"
    assert COOKIE_VALUE not in out.model_dump_json()


def test_unreachable_app_is_check_failure() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _App)
    port = server.server_address[1]
    server.server_close()  # 닫힌 포트
    out = smoke_test(
        SmokeTestInput(run_id=RUN, target="local"), _local_ctx(f"http://127.0.0.1:{port}")
    )
    assert out.passed is False
    assert all(s.detail.startswith("연결 실패") for s in out.scenarios)


def test_local_address_comes_from_inventory_or_fails() -> None:
    with pytest.raises(DdakToolError) as caught:
        smoke_test(SmokeTestInput(run_id=RUN, target="local"), _local_ctx(None))
    assert caught.value.code is ErrorCode.CONFIG_INVALID


def test_local_address_falls_back_to_public_env(app_url: str) -> None:
    platform = {"onprem": {"tiers": {"was": {"public_env": {"APP_BASE_URL": app_url}}}}}
    ctx = RunContext(run_id=RUN, adapter_mode=AdapterMode.REAL, platform=platform)
    assert smoke_test(SmokeTestInput(run_id=RUN, target="local"), ctx).passed is True


@pytest.mark.parametrize("domain", [None, "https://x.example", "bad domain", "localhost"])
def test_cloud_requires_valid_domain(domain: str | None) -> None:
    ctx = RunContext(run_id=RUN, adapter_mode=AdapterMode.REAL, cloud_domain=domain)
    with pytest.raises(DdakToolError) as caught:
        smoke_test(SmokeTestInput(run_id=RUN, target="cloud"), ctx)
    assert caught.value.code is ErrorCode.CONFIG_INVALID


def test_expired_deadline_is_timeout(app_url: str) -> None:
    ctx = _local_ctx(app_url, deadline=time.monotonic() - 1)
    with pytest.raises(DdakToolError) as caught:
        smoke_test(SmokeTestInput(run_id=RUN, target="local"), ctx)
    assert caught.value.code is ErrorCode.ADAPTER_TIMEOUT


# ---- 신뢰하지 않는 응답: 깊은 JSON, 다른 호스트 리다이렉트, 느린 응답, 전체 시간 -----------------


def _hostile_run(responses: Mapping[tuple[str, str], Response]) -> SmokeTestOutput:
    class Hostile(FakeFlaskr):
        def request(
            self, method: str, path: str, form: Mapping[str, str] | None, timeout: float
        ) -> Response:
            if (method, path) in responses:
                return responses[(method, path)]
            return super().request(method, path, form, timeout)

    class Adapter(FakeSmokeAdapter):
        def client(self, ctx: RunContext) -> Hostile:
            return Hostile(ctx.run_id)

    return run_smoke(
        Adapter(Target.LOCAL), SmokeTestInput(run_id=RUN, target="local"), RunContext(RUN)
    )


@pytest.mark.parametrize("body", ["[" * 100_000, '{"a":' * 50_000 + "1" + "}" * 50_000])
def test_deeply_nested_json_is_format_failure_not_crash(body: str) -> None:
    out = _hostile_run({("GET", "/version"): Response(200, (), body)})
    version = out.scenarios[0]
    assert out.passed is False
    assert version.ok is False and version.detail.startswith("응답 형식 오류")


def test_brackets_inside_json_strings_do_not_count_as_depth() -> None:
    version = {
        "release_id": RUN,
        "schema_expected": "0001",
        "app_env": "[" * 100 + '\\"{' * 100,
        "db": {"dialect": "mysql"},
    }
    out = _hostile_run({("GET", "/version"): Response(200, (), json.dumps(version))})
    assert out.scenarios[0].ok is True


@pytest.mark.parametrize(
    "location", ["https://evil.example/", "//evil.example/", "http://evil.example:8080/"]
)
def test_redirect_to_other_host_fails_without_leaking_host(location: str) -> None:
    class Redirecting(FakeFlaskr):
        def request(
            self, method: str, path: str, form: Mapping[str, str] | None, timeout: float
        ) -> Response:
            resp = super().request(method, path, form, timeout)
            if method == "POST" and resp.status == 302:
                return Response(302, (("Location", location),), "")
            return resp

    class Adapter(FakeSmokeAdapter):
        def client(self, ctx: RunContext) -> Redirecting:
            return Redirecting(ctx.run_id)

    out = run_smoke(
        Adapter(Target.LOCAL), SmokeTestInput(run_id=RUN, target="local"), RunContext(RUN)
    )
    create = next(s for s in out.scenarios if s.id == "B2.create")
    assert create.ok is False
    assert create.normalized["location"] == "<external>"
    assert "evil" not in out.model_dump_json()


class _Slow(BaseHTTPRequestHandler):
    """헤더는 바로 보내고 본문은 1바이트씩 천천히 보낸다(소켓 연산마다의 timeout은 넘지 않음)."""

    def log_message(self, *args: object) -> None:
        del args

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "1000")
        self.end_headers()
        try:
            for _ in range(1000):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass


def test_slow_drip_response_is_bounded_by_request_timeout() -> None:
    from ddak.verify.smoke.logic import UrlClient

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = UrlClient(f"http://127.0.0.1:{server.server_address[1]}")
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            client.request("GET", "/", None, 1.0)
        assert time.monotonic() - started < 3.0
    finally:
        server.shutdown()
        server.server_close()


def test_whole_smoke_run_has_its_own_time_limit(
    app_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ctx.deadline이 없으면 SMOKE_TOTAL_S가 전체 시간을 막는다(있으면 실행기 값을 그대로 쓴다)
    monkeypatch.setattr("ddak.verify.smoke.logic.SMOKE_TOTAL_S", 0.0)
    with pytest.raises(DdakToolError) as caught:
        smoke_test(SmokeTestInput(run_id=RUN, target="local"), _local_ctx(app_url))
    assert caught.value.code is ErrorCode.ADAPTER_TIMEOUT


def test_executor_deadline_is_not_shortened(app_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # 실행기가 카탈로그 시간으로 준 ctx.deadline을 smoke가 자기 상한으로 줄이지 않는다
    monkeypatch.setattr("ddak.verify.smoke.logic.SMOKE_TOTAL_S", 0.0)
    ctx = _local_ctx(app_url, deadline=time.monotonic() + 30)
    assert smoke_test(SmokeTestInput(run_id=RUN, target="local"), ctx).passed is True


class _V2Flaskr(FakeFlaskr):
    """앱 태그 v2처럼 목록 페이지에 이미지·박스를 붙인다."""

    BOX = (
        '<section class="release-box" style="display: flex;">'
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><path d="M32 6z"/></svg>'
        '<p style="margin: 0;">v2: image and box added</p></section>'
    )

    def request(
        self, method: str, path: str, form: Mapping[str, str] | None, timeout: float
    ) -> Response:
        resp = super().request(method, path, form, timeout)
        if method == "GET" and path == "/" and resp.status == 200:
            return Response(200, resp.headers, self.BOX + resp.body)
        return resp


def _run_groups(client: FakeFlaskr, groups: list[str]) -> SmokeTestOutput:
    class Adapter(FakeSmokeAdapter):
        def client(self, ctx: RunContext) -> FakeFlaskr:
            return client

    inp = SmokeTestInput(run_id=RUN, target="local", scenarios=groups)
    return run_smoke(Adapter(Target.LOCAL), inp, RunContext(RUN))


def test_v2_group_passes_on_v2_app_after_base() -> None:
    out = _run_groups(_V2Flaskr(RUN), ["base", "v2"])
    assert out.passed is True
    assert [s.id for s in out.scenarios] == [*BASE_IDS, "V2.box"]
    assert out.scenarios[-1].normalized == {"status": 200, "box": True, "image": True}


def test_v2_group_fails_on_v1_app() -> None:
    # v1으로 되돌린 run에서 v2 묶음을 고르면 실패해야 한다(되돌림 run은 base만 고른다)
    out = _run_groups(FakeFlaskr(RUN), ["base", "v2"])
    assert out.passed is False
    v2 = out.scenarios[-1]
    assert v2.id == "V2.box" and v2.ok is False
    assert v2.normalized == {"status": 200, "box": False, "image": False}


def test_v2_box_needs_the_image_inside_the_box() -> None:
    class NoImage(_V2Flaskr):
        BOX = '<section class="release-box"><p>v2: image and box added</p></section><svg></svg>'

    v2 = _run_groups(NoImage(RUN), ["v2"]).scenarios[0]
    assert v2.ok is False
    assert v2.normalized == {"status": 200, "box": True, "image": False}


# ---- S13 대체(긴 제목)·정규화 화면 지문 -------------------------------------------


def test_long_title_without_app_validation_fails() -> None:
    class NoLengthCheck(FakeFlaskr):
        def request(
            self, method: str, path: str, form: Mapping[str, str] | None, timeout: float
        ) -> Response:
            if method == "POST" and len((form or {}).get("title", "")) > 200:
                return Response(302, (("Location", "/"),), "")  # 비엄격 DB에 잘린 채 저장
            return super().request(method, path, form, timeout)

    long = next(s for s in _run_groups(NoLengthCheck(RUN), ["base"]).scenarios if s.id == "B2.long")
    assert long.ok is False
    assert long.normalized == {"status": 302, "error_shown": False}


class _EnvFlaskr(FakeFlaskr):
    """환경별로 주소·날짜·글 id·기존 글이 다른 v1 화면."""

    def __init__(self, run_id: str, origin: str, day: str, others: int) -> None:
        super().__init__(run_id)
        self._origin, self._day, self._others = origin, day, others

    def request(
        self, method: str, path: str, form: Mapping[str, str] | None, timeout: float
    ) -> Response:
        resp = super().request(method, path, form, timeout)
        if method != "GET" or path != "/" or resp.status != 200:
            return resp
        head = (
            f'<link rel="canonical" href="{self._origin}/"><nav><a href="/">Flaskr</a></nav>'
            f'<header><h1>Posts</h1><a href="{self._origin}/create">New</a></header>'
        )
        posts = "".join(
            f'<article class="post"><h1>{t}</h1><div class="about">on {self._day}</div>'
            f'<a href="/{n + 7}/update">Edit</a></article><hr>'
            for n, t in enumerate([*reversed(self._titles), *["old"] * self._others])
        )
        return Response(200, resp.headers, head + posts)


def _create(out: SmokeTestOutput) -> Mapping[str, object]:
    return next(s for s in out.scenarios if s.id == "B2.create").normalized


def test_page_fingerprints_ignore_origin_date_id_and_other_posts() -> None:
    local = _create(
        _run_groups(_EnvFlaskr(RUN, "http://localhost:8080", "2026-10-03", 40), ["base"])
    )
    cloud = _create(_run_groups(_EnvFlaskr(RUN, "https://app.example", "2026-10-04", 0), ["base"]))
    assert str(local["page.head"]).startswith("sha256:")
    assert local["page.head"] == cloud["page.head"]
    assert local["page.post"] == cloud["page.post"] is not None


def test_page_fingerprint_catches_template_difference() -> None:
    class OtherTemplate(_EnvFlaskr):
        def request(
            self, method: str, path: str, form: Mapping[str, str] | None, timeout: float
        ) -> Response:
            resp = super().request(method, path, form, timeout)
            return Response(resp.status, resp.headers, resp.body.replace("New", "Write"))

    a = _create(_run_groups(_EnvFlaskr(RUN, "http://a", "2026-10-03", 1), ["base"]))
    b = _create(_run_groups(OtherTemplate(RUN, "http://a", "2026-10-03", 1), ["base"]))
    assert a["page.head"] != b["page.head"]
    assert a["page.post"] == b["page.post"]


# ---- 클라우드 헬스와 같은 준비 판정, passed == all(ok) ---------------------------


class _ReadyBody(FakeFlaskr):
    def __init__(self, run_id: str, status: int, body: object) -> None:
        super().__init__(run_id)
        self._ready = (status, body)

    def request(
        self, method: str, path: str, form: Mapping[str, str] | None, timeout: float
    ) -> Response:
        if path == "/health/ready":
            status, body = self._ready
            return Response(status, (), json.dumps(body))
        return super().request(method, path, form, timeout)


@pytest.mark.parametrize(
    ("status", "body", "ok"),
    [
        (200, {"status": "ok"}, True),
        (200, {"status": "ready"}, True),
        (200, {"ready": True}, True),
        (200, {"status": "starting"}, False),
        (200, {"ready": "true"}, False),  # 문자열 "true"는 인정하지 않는다(헬스와 같음)
        (200, {"status": {"nested": "ok"}}, False),  # 신뢰하지 않는 값: 예외 없이 실패
        (503, {"status": "ok"}, False),  # 헬스도 200이 아니면 실패
    ],
)
def test_ready_matches_cloud_health_criteria(status: int, body: object, ok: bool) -> None:
    out = _run_groups(_ReadyBody(RUN, status, body), ["base"])
    ready = next(s for s in out.scenarios if s.id == "S0.ready")
    assert ready.ok is ok
    assert out.passed is ok  # 나머지 시나리오는 통과하므로 passed는 ready만 따라간다


@pytest.mark.parametrize("groups", [["base"], ["base", "v2"], ["v2"]])
@pytest.mark.parametrize("app", ["v1", "v2"])
def test_passed_is_exactly_all_scenarios_ok(groups: list[str], app: str) -> None:
    # C-10 약속: passed == (시나리오가 하나 이상 있고 모두 ok)
    client = FakeFlaskr(RUN) if app == "v1" else _V2Flaskr(RUN)
    out = _run_groups(client, groups)
    assert out.scenarios
    assert out.passed is all(s.ok for s in out.scenarios)


def test_no_scenarios_is_never_passed() -> None:
    # 빈 묶음은 입력에서 막히지 않지만(scenarios=[]), 시나리오가 없으면 passed는 False다
    out = run_smoke(
        FakeSmokeAdapter(Target.LOCAL),
        SmokeTestInput(run_id=RUN, target="local", scenarios=[]),
        RunContext(RUN),
    )
    assert out.scenarios == [] and out.passed is False
