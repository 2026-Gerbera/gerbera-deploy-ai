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
BASE_IDS = ["S0.version", "S0.ready", "B1", "B2.create", "B2.empty"]
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
