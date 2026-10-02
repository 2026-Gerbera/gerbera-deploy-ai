"""plan/flow: 접수~검증 통합. 로컬 file:// git 저장소, 가짜 Jev/Provider, 네트워크 없음."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import ddak.app as app_mod
from ddak.app import load_tools
from ddak.core import runtime
from ddak.core.ai.providers.jev import JevAnswer, JevQuestion
from ddak.core.config import Settings
from ddak.core.contracts.base import RUN_ID_PATTERN
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.enums import By, LLMBackend, RunMode
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Plan, Planner
from ddak.core.contracts.plan_draft import PlanDraft, StepDecision
from ddak.core.contracts.tools.generate_plan import GeneratePlanInput
from ddak.core.registry import spec_for
from ddak.executor.engine import check_signals
from ddak.plan import PlanBundle, new_run_id, plan_deployment
from ddak.plan import flow as flow_mod
from ddak.plan.detect import facts_reader
from ddak.plan.intake import FetchPolicy

load_tools()

DEPLOY = """\
tiers:
  web:
    paths: [web]
    dockerfile: web/Dockerfile
  was:
    paths: [was]
    dockerfile: null
migrations_dir: migrations
env_example: .env.example
"""
V1: dict[str, str] = {
    "deploy.yaml": DEPLOY,
    "web/Dockerfile": "FROM nginx\n",
    "was/app.py": 'import os\nH = os.environ.get("DB_HOST")\n',
    "migrations/0001_init.sql": "create table t(a int);\n",
    ".env.example": "DB_HOST=\n",
}
V2: dict[str, str] = {
    **V1,
    "was/app.py": (
        'import os\nH = os.environ.get("DB_HOST")\nS = os.environ["SECRET_KEY"]\n'
        'C = os.getenv("SESSION_COOKIE_SECURE")\n'
    ),
    "migrations/0002_add.sql": "alter table t add b int;\n",
    ".env.example": "DB_HOST=\nSECRET_KEY=\nSESSION_COOKIE_SECURE=\n",
}
GENV = {"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
SETTINGS = Settings(ai_retries=0, llm_backend=LLMBackend.API, llm_model="m", jev_model="m")


class FakeJev:
    def __init__(self, prob: float = 0.1) -> None:
        self.prob = prob
        self.calls: list[Sequence[JevQuestion]] = []

    def ask(self, *, state: str, questions: Sequence[JevQuestion]) -> list[JevAnswer]:
        self.calls.append(questions)
        return [JevAnswer(id=q.id, probability=self.prob) for q in questions]


def commit(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=repo, env=GENV, check=True, timeout=30
        )
    for name, body in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    for args in (["add", "-A"], ["commit", "-q", "-m", "c", "--allow-empty"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=repo, env=GENV, check=True, capture_output=True, timeout=30,
        )  # fmt: skip


def policy(tmp: Path) -> FetchPolicy:
    return FetchPolicy(
        allowed_schemes=("file",), allowed_hosts=None, root=tmp / "src", timeout_s=20.0
    )


def request(repo: Path, **kw: Any) -> DeployRequest:
    base: dict[str, Any] = dict(
        project="demo", repo_url=f"file://{repo}", target="both", mode=RunMode.UPDATE
    )
    return DeployRequest(**(base | kw))


def none_prev(project: str) -> dict[str, None]:
    return {"local": None, "cloud": None}


def go(tmp: Path, repo: Path, run_id: str = "run-1", **kw: Any) -> PlanBundle:
    kw.setdefault("previous_manifests", none_prev)
    kw.setdefault("jev_client", FakeJev())
    req = kw.pop("request", None) or request(repo, mode=RunMode.BOOTSTRAP)
    return plan_deployment(req, run_id=run_id, settings=SETTINGS, fetch_policy=policy(tmp), **kw)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "remote"
    commit(r, V1)
    return r


# ---- T2 ----------------------------------------------------------------------------------------
def test_new_run_id() -> None:
    ids = {new_run_id() for _ in range(20)}
    assert len(ids) > 1 and all(re.fullmatch(RUN_ID_PATTERN, i) for i in ids)
    fixed = new_run_id(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    assert re.fullmatch(r"run-20260102-030405-[0-9a-f]{4}", fixed)


# ---- T3 + 시간 로그 ----------------------------------------------------------------------------
def test_normal_path_and_stage_timing(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    t0 = time.perf_counter()
    b = go(tmp_path, repo)
    assert time.perf_counter() - t0 < 10  # 접수~검증이 단일 자릿수 초(AI 지연 제외)
    Plan.model_validate(b.plan.model_dump(by_alias=True))
    check_signals(b.plan)
    assert b.plan.facts_hash == b.facts.facts_hash == facts_reader(b.source)
    assert b.source.is_dir()  # 성공하면 보존
    assert b.context.run_id == b.plan.run_id == "run-1"
    assert b.context.toggles == b.plan.toggles == {"code_patch": False, "strict_ai_check": False}
    assert b.context.deploy_config["tiers"]["web"]["dockerfile"] == "web/Dockerfile"
    assert b.context.images == {} and b.context.cloud_domain is None
    json.dumps(b.to_json_dict())  # 직렬화 가능
    stages = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("{") and '"stage"' in line
    ]
    done = [s["stage"] for s in stages if s["msg"] == "플랜 단계 완료"]
    assert done == ["receive", "detect", "analyze", "plan", "validate"]
    assert all(isinstance(s["ms"], int) for s in stages)


def test_context_values_passed_through(tmp_path: Path, repo: Path) -> None:
    b = go(tmp_path, repo, cloud_domain="example.test", platform={"onprem": {"x": 1}})
    assert b.context.cloud_domain == "example.test" and b.context.platform == {"onprem": {"x": 1}}


# ---- 재지시·폴백·모드 (T4, T5, T5c) -------------------------------------------------------------
def bad_draft() -> PlanDraft:
    return PlanDraft(
        decisions=(StepDecision(id="deploy.nope.cloud", include=True, reason="x"),),
        planner=Planner(by=By.AI, provider="replay"),
    )


def good_draft() -> PlanDraft:
    return PlanDraft(decisions=(), planner=Planner(by=By.AI, provider="replay"))


class Gen:
    """flow.generate_plan 대체: 초안을 차례로 돌려주고 호출(feedback, 문맥)을 적는다."""

    def __init__(self, drafts: list[PlanDraft]) -> None:
        self.drafts, self.calls = drafts, []

    def __call__(self, inp: GeneratePlanInput, ctx: Any, **kw: Any) -> Any:
        self.calls.append((inp.feedback, runtime.current_tool.get(), runtime.current_run_id.get()))
        return type("Out", (), {"draft": self.drafts[min(len(self.calls), len(self.drafts)) - 1]})


def test_strict_retry_once_then_ok(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen = Gen([bad_draft(), good_draft()])
    monkeypatch.setattr(flow_mod, "generate_plan", gen)
    b = go(tmp_path, repo, strict_ai_check=True)
    assert len(gen.calls) == 2 and gen.calls[0][0] == () and gen.calls[1][0]
    assert b.plan.planner and b.plan.planner.fallback is False and b.plan.planner.by is By.AI
    assert b.plan.toggles["strict_ai_check"] is True is b.context.toggles["strict_ai_check"]


def test_strict_falls_back_to_rule_plan(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen = Gen([bad_draft(), bad_draft()])
    monkeypatch.setattr(flow_mod, "generate_plan", gen)
    b = go(tmp_path, repo, strict_ai_check=True)
    assert len(gen.calls) == 2
    assert b.plan.planner and b.plan.planner.by is By.RULE and b.plan.planner.fallback is True
    Plan.model_validate(b.plan.model_dump(by_alias=True))


def test_default_check_never_reprompts(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen = Gen([bad_draft(), good_draft()])
    monkeypatch.setattr(flow_mod, "generate_plan", gen)
    b = go(tmp_path, repo)  # 기본 검사
    assert len(gen.calls) == 1
    assert b.plan.toggles == dict(b.context.toggles) and b.plan.toggles["strict_ai_check"] is False


# ---- T5b 실패 정리 ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name", ["detect_changed_tiers", "analyze_project", "generate_plan", "validate_plan"]
)
def test_failure_after_receive_removes_checkout(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("boom-" + name)

    monkeypatch.setattr(flow_mod, name, boom)
    with pytest.raises(RuntimeError, match="boom-" + name):
        go(tmp_path, repo)
    assert not (tmp_path / "src" / "runs" / "demo" / "run-1").exists()


def test_receive_failure_leaves_nothing_and_propagates(tmp_path: Path, repo: Path) -> None:
    commit(repo, {"deploy.yaml": "tiers: {}\n"})
    with pytest.raises(DdakToolError) as e:
        go(tmp_path, repo)
    assert e.value.code is ErrorCode.CONFIG_INVALID
    assert not (tmp_path / "src" / "runs" / "demo" / "run-1").exists()


# ---- T5d 정리 호출 ------------------------------------------------------------------------------
def test_cleanup_called_with_keep(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        flow_mod, "cleanup_stale_sources", lambda *a, **k: seen.append((a, k)) or []
    )
    b = go(tmp_path, repo, run_id="run-keep")
    assert seen == [((tmp_path / "src", 3600.0), {"keep": ("run-keep",)})]
    assert b.source.is_dir()


def test_stale_run_removed_and_new_run_kept(tmp_path: Path, repo: Path) -> None:
    stale = tmp_path / "src" / "runs" / "demo" / "old-run"
    stale.mkdir(parents=True)
    old = time.time() - 7200
    os.utime(stale, (old, old))
    b = go(tmp_path, repo, run_id="new-run")
    assert not stale.exists() and b.source.is_dir()


# ---- T6 AI 가드 ---------------------------------------------------------------------------------
def test_ai_guard_and_tool_names(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ddak.plan.analyze import analyze_project
    from ddak.plan.planner import generate_plan

    seen: list[str | None] = []

    def spy(real: Any) -> Any:
        def wrapper(*a: Any, **k: Any) -> Any:
            seen.append(runtime.current_tool.get())
            return real(*a, **k)

        return wrapper

    monkeypatch.setattr(flow_mod, "analyze_project", spy(analyze_project))
    monkeypatch.setattr(flow_mod, "generate_plan", spy(generate_plan))
    b = go(tmp_path, repo)
    assert seen == ["analyze_project", "generate_plan"]
    assert runtime.current_tool.get() is None  # 문맥은 끝나면 되돌려진다
    inp = GeneratePlanInput(run_id="run-1", facts=b.facts)
    with pytest.raises(DdakToolError) as e:  # tool_context 없이는 AI 단계가 동작하지 않는다
        generate_plan(inp, b.context, jev_client=FakeJev(), settings=SETTINGS)
    assert e.value.code is ErrorCode.AI_NOT_ALLOWED


def test_flow_does_not_import_ai_or_internals() -> None:
    src = Path(flow_mod.__file__).read_text()
    assert not re.search(r"^\s*(from|import) ddak\.core\.ai", src, re.M)
    assert not re.search(r"from ddak\.plan\.\w+\.\w+ import", src)


# ---- T10b 감시 연결 -----------------------------------------------------------------------------
def test_watch_disabled_without_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DDAK_WATCH_REPO_URL", raising=False)
    monkeypatch.setenv("DDAK_RUN_DIR", str(tmp_path / "runs"))
    built: list[Any] = []
    monkeypatch.setattr(app_mod, "Watcher", lambda *a, **k: built.append(1))
    app = app_mod.create()

    async def life() -> None:
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(life())
    assert built == []


def test_watch_new_commit_runs_plan_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DDAK_WATCH_REPO_URL", "https://github.com/o/r")
    monkeypatch.setenv("DDAK_RUN_DIR", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("DDAK_SOURCES_DIR", str(tmp_path / "src"))
    calls: list[dict[str, Any]] = []
    stopped: list[bool] = []

    def fake_plan(req: DeployRequest, **kw: Any) -> Any:
        calls.append({"req": req, "prev": kw["previous_manifests"](req.project), **kw})
        source = tmp_path / "application"
        source.mkdir()
        (source / "app.py").write_text("version = 1\n")
        p = Plan.model_validate(
            {
                "run_id": kw["run_id"],
                "project": req.project,
                "mode": req.mode,
                "deploy": {
                    target: {
                        "steps": [
                            {
                                "id": f"verify.health.{target}",
                                "tool": "health_check",
                                "target": target,
                                "layer": spec_for("health_check").layer,
                                "effect": spec_for("health_check").effect,
                            }
                        ]
                    }
                    for target in ("local", "cloud")
                },
            }
        )
        return PlanBundle(
            plan=p,
            context=RunContext(p.run_id, project=p.project, mode=p.mode),
            facts=None,
            source=source,  # type: ignore[arg-type]
        )

    class FakeWatcher:
        def __init__(self, targets: list[Any], handler: Any, *, policy: FetchPolicy) -> None:
            self.targets, self.handler, self.ev = targets, handler, asyncio.Event()

        async def run(self) -> None:
            await self.handler(self.targets[0], "a" * 40)  # 새 커밋 하나
            await self.ev.wait()
            stopped.append(True)

        async def stop(self) -> None:
            self.ev.set()

    monkeypatch.setattr(app_mod, "plan_deployment", fake_plan)
    monkeypatch.setattr(app_mod, "Watcher", FakeWatcher)
    # 이 시험은 watcher→계획 연결만 격리한다. checkout은 로컬 bare E2E에서 검증한다.
    monkeypatch.setattr(app_mod, "_repository_factory", lambda root: None)
    app = app_mod.create()

    async def life() -> asyncio.Task[Any]:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.2)
            return next(t for t in asyncio.all_tasks() if t.get_name() == "repo-watch")

    task = asyncio.run(life())
    assert task.done() and stopped == [True]  # 종료 때 감시 task 정리
    assert len(calls) == 1
    req = calls[0]["req"]
    assert (req.project, req.mode, req.repo_url) == (
        "flaskr", RunMode.BOOTSTRAP, "https://github.com/o/r"
    )  # fmt: skip
    assert calls[0]["prev"] == {"local": None, "cloud": None}
    assert re.fullmatch(RUN_ID_PATTERN, calls[0]["run_id"])
    assert "새 커밋 계획 생성" in capsys.readouterr().err
