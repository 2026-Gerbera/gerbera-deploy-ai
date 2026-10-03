"""Docker 로그인·저장소 읽기·승인 전 차단. 모든 외부 경계는 가짜다."""

import io
import json
import subprocess
import urllib.error
from html.parser import HTMLParser

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ddak.cloud.build import local
from ddak.core import setup_tools
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError
from ddak.core.docker_auth import docker_hub_auth
from ddak.web.routes.setup import router
from tests.unit import test_deployment_service as deploy_support
from tests.unit import test_setup_service_fix11 as service_support
from tests.unit import test_setup_tools_fix11 as tool_support

rig = service_support.rig
deploy_rig = deploy_support.rig
prepared = tool_support.prepared
deny_live_io = tool_support.deny_live_io


class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            attrs = dict(attrs)
            self.inputs[attrs.get("name")] = attrs


@pytest.mark.parametrize("case", ["success", "username", "auth", "network", "read-denied"])
def test_login_and_read_probe_update_ui_without_exposing_token(rig, monkeypatch, case):
    service, manager, _ = rig
    service.save_project_settings(
        "flaskr",
        {"image_repository": "2026gerbera/flaskr", "build_backend": "local"},
        updated_by="fixture",
        expected_version=0,
    )
    fake = tool_support.FakeRunner()
    token = "fixture-" + "private-login"

    def runner(argv, **kwargs):
        if case == "auth":
            return subprocess.CompletedProcess(argv, 1, "", "unauthorized " + token)
        if case == "network":
            raise subprocess.TimeoutExpired(argv, 30, output=token)
        return fake(argv, **kwargs)

    manager._build_factory = lambda root, **kw: setup_tools.BuildSetup(root, runner=runner, **kw)
    reads = []

    class Opener:
        def open(self, request, timeout):
            reads.append(request)
            assert request.get_method() == "GET" and timeout == 10
            assert token not in request.full_url
            if len(reads) == 2 and case == "read-denied":
                raise urllib.error.HTTPError(request.full_url, 403, token, {}, None)
            response = io.BytesIO(json.dumps({"token": "fixture-bearer"}).encode())
            response.status = 200
            return response

    monkeypatch.setattr(setup_tools.urllib.request, "build_opener", lambda *args: Opener())
    app = FastAPI()
    app.state.settings, app.state.deployment = Settings(), service
    app.include_router(router)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        html = client.get("/setup?project=flaskr").text
        inputs = Inputs(html).inputs
        assert inputs["username"]["value"] == "2026gerbera"
        assert "required" in inputs["username"]
        response = client.post(
            "/setup/docker",
            data={
                "project": "flaskr",
                "csrf_token": client.cookies["ddak_csrf"],
                "username": "" if case == "username" else "personal-user",
                "token": token,
                "_form_id": "setup-docker",
                "_return_to": "/setup?project=flaskr",
            },
            headers={"Origin": "http://127.0.0.1:8765", "Accept": "text/html", "X-Ddak-Form": "1"},
            follow_redirects=False,
        )
        assert token not in response.text
        check = next(x for x in manager.view("flaskr")["checklist"] if x["id"] == "docker")
        assert token not in repr(check)
        if case == "success":
            assert response.status_code == 303 and check["status"] == "green"
            assert check["verified_at"] and "읽기 확인됨" in check["detail"]
        else:
            assert response.status_code == 409 and check["status"] == "red"
            message = response.json()["error"]["message"]
            assert {
                "username": "사용자명",
                "auth": "인증 실패",
                "network": "시간 초과",
                "read-denied": "읽기 권한 실패",
            }[case] in message
        assert len(reads) == (2 if case in {"success", "read-denied"} else 0)
        if reads:
            assert "repository%3A2026gerbera%2Fflaskr%3Apull" in reads[0].full_url
            assert reads[1].full_url.endswith("/2026gerbera/flaskr/tags/list?n=1")
        page = client.get("/setup?project=flaskr").text
        checklist = page.split('data-check="docker"', 1)[1].split("</div>", 1)[0]
        assert ('data-status="green"' if case == "success" else 'data-status="red"') in checklist
        assert token not in page


def test_empty_auth_blocks_prepare_before_runner_and_approval(prepared, deploy_rig):
    builder, runner, _ = prepared
    tool_support.apply(builder)  # 빈 자동 helper 방지 항목만 생성된다.
    runner.calls.clear()
    service, source, _ = deploy_rig
    plan = deploy_support.plan()
    service.build_preflight = lambda ctx: local.preflight_local_build(
        ctx.image_repository, runner=runner, config=builder.plan()["paths"]
    )
    with pytest.raises(DdakToolError, match="설정 필요: Docker Hub 로그인"):
        service.prepare(
            plan,
            RunContext(
                plan.run_id,
                project=plan.project,
                build_backend="local",
                image_repository="2026gerbera/flaskr",
            ),
            source,
        )
    assert not service.list_runs() and not runner.calls


@pytest.mark.parametrize("key", ["auth", "identitytoken"])
def test_product_auth_requires_nonempty_credential(key):
    host = "https://index.docker.io/v1/"
    assert not docker_hub_auth({"auths": {host: {}}})
    assert not docker_hub_auth({"auths": {host: {key: " "}}})
    assert docker_hub_auth({"auths": {host: {key: "fixture-credential"}}})
