"""source=fixture: 대시보드 코드 질문. 임시 git 저장소와 가짜 AI provider만 쓴다.

실제 AI·네트워크 호출은 없다.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ddak import app as assembly
from ddak.core.ai import gateway
from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.code_context import collect_context, is_secret_path, question_terms
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.step_catalog import catalog_steps
from ddak.core.contracts.tools.answer_code_question import (
    MAX_CONTEXT_BYTES,
    AnswerCodeQuestionInput,
    CodeFile,
)
from ddak.core.redact import REDACTED
from ddak.web.app import create_app
from tests.unit import test_deployment_service as support
from tests.unit.core.test_app_repository import git

rig = support.rig
BASE = "http://127.0.0.1:8765"
PROJECT = "flaskr"
TOKEN = "c" * 43
FAKE_SECRET = "hunter" + "2-fixture-value"  # gitleaks에 걸리지 않게 이어 붙인 가짜 값
FAKE_KEY_ID = "AKIA" + "ABCDEFGHIJKLMNOP"
SECRET_FILES = {
    ".env": f"SECRET_KEY={FAKE_SECRET}\n",
    "config/.env.production": "login=1\n",
    ".secrets/login_token.txt": "login\n",
    "deploy/login.pem": "login pem body\n",
    "infra/terraform.tfstate": '{"login": 1}\n',
    "keys/id_rsa": "login key body\n",
}


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.seen: list[AIRequest] = []

    def complete(self, req: AIRequest) -> AIResponse:
        self.seen.append(req)
        reply = {
            "answer": "login은 app.py의 login 함수가 처리합니다.",
            "sources": ["app.py", "x.py"],
        }
        return AIResponse(text=json.dumps(reply, ensure_ascii=False), source=Source.LIVE)


@pytest.fixture
def origin(tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", bare.as_uri(), str(seed))
    files = {
        "README.md": "# flaskr\n게시판 샘플 앱\n",
        "app.py": f'def login():\n    password = "{FAKE_SECRET}"\n    return "{FAKE_KEY_ID}"\n',
        "flaskr/__init__.py": "def create_app():\n    return None\n",
        "flaskr/blog.py": "def create_post():\n    pass  # login required\n",
        "Dockerfile": "FROM python:3.12-slim\n",
        "requirements.txt": "flask\n",
        "docs/big.txt": "big " * 50_000,
        **SECRET_FILES,
    }
    for name, body in files.items():
        (seed / name).parent.mkdir(parents=True, exist_ok=True)
        (seed / name).write_text(body)
    (seed / "logo.png").write_bytes(b"\x89PNG\0\0login")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "Seed")
    git(seed, "push", "origin", "HEAD:prod")
    factory = assembly._repository_factory(tmp_path / "repositories", allow_local=True)
    return bare, factory


class Service:
    def __init__(self, bare, factory) -> None:
        self.registry = assembly.load_tools()
        self.onboarding = None
        self.saved = {"repo_url": bare.as_uri(), "watch_branch": "prod"}
        self.factory = factory

    def get_project_settings(self, project):
        return self.saved

    def connect_repository(self, ctx: RunContext):
        return self.factory(ctx)


def _checkout(bare, factory):
    repo = factory(RunContext("qa-test", project=PROJECT, repo_url=bare.as_uri(), ref="prod"))
    repo.git("fetch", "--no-tags", "origin", "refs/heads/prod:refs/remotes/origin/prod")
    return repo, repo.git("rev-parse", "refs/remotes/origin/prod^{commit}")


@pytest.mark.parametrize(
    "path",
    [".env", "config/.env.local", "prod.env", ".secrets/a.txt", "a/key.pem", "server.key",
     "terraform.tfstate", "infra/terraform.tfstate.backup", "id_rsa", "home/id_ed25519.pub",
     ".aws/credentials", "secrets.yaml", "prod.tfvars"],
)  # fmt: skip
def test_secret_paths(path):
    assert is_secret_path(path)


@pytest.mark.parametrize(
    "path", ["app.py", "environment.py", "keyboard.py", "docs/secret_santa.md"]
)
def test_plain_paths(path):
    assert not is_secret_path(path)


def test_context_excludes_secret_paths_redacts_and_ranks(origin):
    repo, sha = _checkout(*origin)
    context = collect_context(repo.git_bytes, sha, "login 처리는 어디서 하나요?")
    listed = {f.path for f in context.files} | set(context.paths)
    assert not listed & set(SECRET_FILES) and context.skipped_secret == len(SECRET_FILES)
    assert context.files[0].path == "app.py"  # 경로·내용 일치 + 진입 파일
    text = "\n".join(f.content for f in context.files)
    assert FAKE_SECRET not in text and FAKE_KEY_ID not in text and REDACTED in text
    assert "logo.png" in context.paths and "logo.png" not in {f.path for f in context.files}
    assert repo.git("ls-files") == ""  # checkout·작업 트리 변경 없음(읽기 명령만)
    assert "login" in question_terms("login 처리는 어디서 하나요?")


def test_context_respects_size_limit(origin):
    repo, sha = _checkout(*origin)
    full = collect_context(repo.git_bytes, sha, "big")
    assert full.total_bytes <= MAX_CONTEXT_BYTES and full.truncated
    assert full.files[0].path == "docs/big.txt" and full.files[0].truncated
    small = collect_context(repo.git_bytes, sha, "big", limit=3000)
    assert small.total_bytes <= 3000 and small.truncated
    with pytest.raises(ValidationError, match="상한"):
        AnswerCodeQuestionInput(
            run_id="qa-1",
            question="q",
            commit="a" * 40,
            branch="prod",
            files=(CodeFile(path="a.py", content="x" * MAX_CONTEXT_BYTES),),
            paths=("a.py",),
        )


def test_assembled_question_uses_registry_tool_and_redacted_input(origin, monkeypatch):
    bare, factory = origin
    provider = FakeProvider()
    monkeypatch.setattr(gateway, "get_provider", lambda settings: provider)
    qa = assembly._code_question_service(Service(bare, factory), Settings())
    result = qa.ask(PROJECT, "login 처리는 어디서 하나요?")
    sha = git(bare, "rev-parse", "prod")
    assert result["commit"] == sha[:7] and result["branch"] == "prod"
    assert result["sources"] == ["app.py"]  # 넣지 않은 경로는 근거로 인정하지 않는다
    sent = provider.seen[0]
    assert sent.purpose == "answer_code_question"
    assert "운영자 요청: login 처리는" in sent.user and gateway.DATA_OPEN in sent.user
    assert "지시" in sent.user.split(gateway.DATA_OPEN)[0]  # 코드 속 지시를 따르지 않는다
    assert FAKE_SECRET not in sent.user and FAKE_KEY_ID not in sent.user
    for name in ("login_token", "id_rsa", "tfstate", ".env", "login.pem"):
        assert name not in sent.user
    assert len(sent.user) > 4096  # 공통 4096자 상한이 아니라 툴 상한으로 넣는다
    with pytest.raises(DdakToolError) as info:
        qa.ask(PROJECT, "   ")
    assert info.value.code is ErrorCode.CONFIG_INVALID


def test_code_question_is_never_a_plan_step():
    for target in ("local", "cloud", "both"):
        assert all(s.tool != "answer_code_question" for s in catalog_steps(("was",), target))


class WebService:
    def __init__(self) -> None:
        self.code_question = self
        self.asked: list[tuple[str, str]] = []

    def resolve_project(self, name):
        return name

    async def shutdown(self):
        return None

    def ask(self, project, question):
        self.asked.append((project, question))
        if question == "boom":
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "AI 호출 실패")
        if question == "crash":
            raise RuntimeError("private detail /home/operator")
        return {"answer": "답", "sources": ["app.py"], "commit": "abcdef1", "branch": "prod"}


def test_web_route_requires_csrf_and_returns_json_errors():
    service = WebService()
    app = create_app(deployment_factory=lambda: service, settings=Settings())
    with TestClient(app, base_url=BASE) as client:

        def ask(question, **headers):
            return client.post(
                "/code-qa/ask",
                data={"csrf_token": TOKEN, "project": PROJECT, "question": question},
                headers={"origin": BASE, "accept": "application/json", **headers},
            )

        rejected = ask("q")
        assert rejected.status_code == 403 and rejected.json()["ok"] is False
        client.cookies.set("ddak_csrf", TOKEN)
        assert ask("q", origin="https://outside.invalid").status_code == 403
        assert service.asked == []
        ok = ask("login?")
        assert ok.status_code == 200 and ok.json()["ok"] and ok.json()["commit"] == "abcdef1"
        failed = ask("boom")
        assert failed.status_code == 409 and failed.json()["error"]["code"] == "AI_UNAVAILABLE"
        crashed = ask("crash")
        assert crashed.status_code == 502 and "private detail" not in crashed.text
        assert client.get("/static/code_qa.js").status_code == 200


def test_dashboard_has_question_button_next_to_deploy_prep(rig, monkeypatch):
    service, _, _ = rig
    monkeypatch.setenv("DDAK_WATCH_PROJECT", PROJECT)
    app = create_app(deployment_factory=lambda: service, settings=Settings())
    with TestClient(app, base_url=BASE) as client:
        html = client.get("/").text
    assert 'action="/ops/plan"' in html and "data-code-qa-toggle" in html
    assert 'data-url="/code-qa/ask"' in html and "/static/code_qa.js" in html
    assert f'data-csrf="{client.cookies.get("ddak_csrf")}"' in html
