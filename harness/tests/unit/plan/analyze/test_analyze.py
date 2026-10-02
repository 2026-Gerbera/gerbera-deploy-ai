"""plan/analyze: 규칙 + Jev 분류, AI 입력 안전, 툴 등록."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from ddak.app import load_tools
from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.ai.providers.claude import ClaudeJevClient
from ddak.core.ai.providers.jev import GroqJevClient, JevAnswer, JevQuestion
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.enums import By, RunMode, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.analyze_project import AnalyzeProjectInput
from ddak.core.runtime import tool_context
from ddak.plan.analyze import analyze_project

CFG = {
    "tiers": {"web": {"paths": ["web"], "dockerfile": "web/Dockerfile"}, "was": {"paths": ["was"]}},
    "env_example": ".env.example",
}
FAKE_VALUE = "hunter" + "2-" + "value"  # gitleaks 회피: 이어 붙여 만든 가짜 값


class FakeJev:
    def __init__(self, prob: float | None = None, exc: Exception | None = None) -> None:
        self.prob, self.exc = prob, exc
        self.calls: list[tuple[str, Sequence[JevQuestion]]] = []

    def ask(self, *, state: str, questions: Sequence[JevQuestion]) -> list[JevAnswer]:
        self.calls.append((state, questions))
        if self.exc:
            raise self.exc
        return [JevAnswer(id=q.id, probability=self.prob) for q in questions]


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def _run(
    tmp_path: Path,
    files: dict[str, str],
    *,
    changed: tuple[str, ...] | None = None,
    jev=None,
    mode=RunMode.UPDATE,
):
    _write(tmp_path / "src1", files)
    inp = AnalyzeProjectInput(
        run_id="run-1",
        request=DeployRequest(project="demo", repo_url="https://github.com/o/r", target="both"),
        source_dir="src1",
        changed={"local": {"web": True, "was": True}},
        changed_paths=tuple(files) if changed is None else changed,
    )
    ctx = RunContext("run-1", deploy_config=CFG, mode=mode)
    with tool_context("analyze_project", "run-1"):
        return analyze_project(inp, ctx, jev_client=jev, root=tmp_path)


def _by_name(out):
    return {k.name: k for k in out.env_keys}


GOLDEN = {
    "web/Dockerfile": "FROM x",
    ".env.example": f"SECRET_KEY={FAKE_VALUE}\nSESSION_COOKIE_SECURE=1\n# C=1\n",
    "was/app.py": (
        'import os\nA = os.environ["SECRET_KEY"]\nB = os.getenv("SESSION_COOKIE_SECURE")\n'
    ),
}


@pytest.mark.parametrize(
    "jev", [FakeJev(0.9), FakeJev(exc=DdakToolError(ErrorCode.AI_UNAVAILABLE, "x"))]
)
def test_golden(tmp_path: Path, jev: FakeJev) -> None:
    out = _run(tmp_path, GOLDEN, jev=jev)
    k = _by_name(out)
    assert (k["SECRET_KEY"].kind, k["SESSION_COOKIE_SECURE"].kind) == ("secret", "plain")
    assert out.infra_inputs_changed and out.tiers == ("web", "was")
    assert out.has_dockerfile == {"web": True, "was": False}
    assert jev.calls == []  # 둘 다 규칙으로 정해져 Jev를 부르지 않는다
    assert out.source is None and out.ai_usage is None


def test_example_only_and_source_regex(tmp_path: Path) -> None:
    files = {
        ".env.example": "export DB_HOST=h\nBAD-NAME=1\n",
        "was/a.py": 'os.environ.get("LOG_LEVEL", "info")\nos.environ["API_TOKEN"]\n',
        "was/.hidden/x.py": 'os.getenv("HIDDEN")',
    }
    k = _by_name(_run(tmp_path, files))
    assert set(k) == {"DB_HOST", "LOG_LEVEL", "API_TOKEN"}
    assert k["API_TOKEN"].tier == "was" and k["DB_HOST"].tier is None
    assert k["API_TOKEN"].kind == "secret" and k["LOG_LEVEL"].by is By.RULE


@pytest.mark.parametrize(("prob", "kind"), [(0.8, "secret"), (0.2, "plain")])
def test_ambiguous_uses_jev(tmp_path: Path, prob: float, kind: str) -> None:
    jev = FakeJev(prob)
    out = _run(tmp_path, {"was/a.py": f'os.getenv("MYSTERY", "{FAKE_VALUE}")\n'}, jev=jev)
    key = _by_name(out)["MYSTERY"]
    assert (key.kind, key.by) == (kind, By.AI)
    assert out.source is Source.LIVE
    state, qs = jev.calls[0]
    assert qs[0].id == "secret.mystery" and qs[0].kind == "noul"
    assert FAKE_VALUE not in state and FAKE_VALUE not in qs[0].text  # AI 입력에 값 없음


def test_jev_unavailable_is_conservative(tmp_path: Path) -> None:
    jev = FakeJev(exc=DdakToolError(ErrorCode.AI_UNAVAILABLE, "down"))
    out = _run(tmp_path, {"was/a.py": 'os.getenv("MYSTERY")'}, jev=jev)
    key = _by_name(out)["MYSTERY"]
    assert key.kind == "secret" and "FakeJev model=unknown 불가" in (key.reason or "")
    assert out.infra_inputs_changed and out.source is None


def test_no_keys(tmp_path: Path) -> None:
    out = _run(tmp_path, {"was/a.py": "x = 1\n"})
    assert out.env_keys == () and not out.infra_inputs_changed


def test_unchanged_example_not_new(tmp_path: Path) -> None:
    out = _run(tmp_path, {".env.example": "SECRET_KEY=\n"}, changed=())
    assert not _by_name(out)["SECRET_KEY"].is_new and not out.infra_inputs_changed


def test_ai_guard_outside_tool_context(tmp_path: Path) -> None:
    _write(tmp_path / "src1", {"was/a.py": 'os.getenv("MYSTERY")'})
    inp = AnalyzeProjectInput(
        run_id="run-1",
        request=DeployRequest(project="demo", repo_url="https://github.com/o/r", target="both"),
        source_dir="src1",
        changed={},
    )
    with pytest.raises(DdakToolError) as info:
        analyze_project(
            inp, RunContext("r", deploy_config=CFG), jev_client=FakeJev(0.9), root=tmp_path
        )
    assert info.value.code is ErrorCode.AI_NOT_ALLOWED


def test_tool_registered() -> None:
    assert "analyze_project" in load_tools().registered()


def test_migration_keys_excluded_from_results_and_questions(tmp_path: Path) -> None:
    jev = FakeJev(0.2)
    files = {
        ".env.example": "DB_USER_MIGRATOR=\nDB_PASSWORD_MIGRATOR=\nMYSTERY=\n",
        "was/a.py": 'os.getenv("MYSTERY_MIGRATOR")\nos.getenv("MYSTERY")\n',
    }
    out = _run(tmp_path, files, jev=jev, mode=RunMode.BOOTSTRAP, changed=())
    assert set(_by_name(out)) == {"MYSTERY"}
    state, qs = jev.calls[0]
    assert "MIGRATOR" not in state and "MIGRATOR" not in str(qs)
    assert _by_name(out)["MYSTERY"].is_new


def test_only_migration_keys_do_not_call_ai(tmp_path: Path) -> None:
    jev = FakeJev(0.9)
    out = _run(tmp_path, {".env.example": "DB_PASSWORD_MIGRATOR=\nMYSTERY_MIGRATOR=\n"}, jev=jev)
    assert out.env_keys == () and not out.infra_inputs_changed and jev.calls == []


@pytest.mark.parametrize("mode,is_new", [(RunMode.BOOTSTRAP, True), (RunMode.UPDATE, False)])
def test_bootstrap_marks_unchanged_example_keys_new(
    tmp_path: Path, mode: RunMode, is_new: bool
) -> None:
    files = {".env.example": "SECRET_KEY=\nDB_HOST=\n", "was/a.py": 'os.getenv("LOG_LEVEL")'}
    out = _run(tmp_path, files, changed=(), mode=mode)
    keys = _by_name(out)
    assert keys["SECRET_KEY"].is_new is is_new and keys["DB_HOST"].is_new is is_new
    assert not keys["LOG_LEVEL"].is_new
    assert out.infra_inputs_changed is is_new


def test_actual_client_provenance_in_reason(tmp_path: Path) -> None:
    class NamedFake(FakeJev):
        name = "claude-cli"
        model = "actual-model"

    out = _run(tmp_path, {".env.example": "MYSTERY=\nSECRET_KEY=\n"}, jev=NamedFake(0.9))
    keys = _by_name(out)
    key = keys["MYSTERY"]
    assert key.reason == "claude-cli model=actual-model"
    assert (key.provider, key.model) == ("claude-cli", "actual-model")
    assert keys["SECRET_KEY"].provider is None and keys["SECRET_KEY"].model is None
    # 준비 단계가 reason을 지워도 구조화한 출처는 JSON에 남는다.
    saved = key.model_copy(update={"reason": None}).model_dump(mode="json")
    assert saved["reason"] is None
    assert (saved["provider"], saved["model"]) == ("claude-cli", "actual-model")


@pytest.mark.parametrize(
    "error",
    [
        DdakToolError(ErrorCode.AI_UNAVAILABLE, "down"),
        DdakToolError(ErrorCode.AI_OUTPUT_INVALID, "invalid"),
        None,  # 확률 누락도 기존 보수 처리와 출처를 유지한다.
    ],
)
def test_failed_judgment_keeps_provider_model(tmp_path: Path, error: Exception | None) -> None:
    class NamedFake(FakeJev):
        name = "claude-cli"
        model = "actual-model"

    out = _run(tmp_path, {".env.example": "MYSTERY=\n"}, jev=NamedFake(exc=error))
    key = _by_name(out)["MYSTERY"]
    assert (key.provider, key.model) == ("claude-cli", "actual-model")
    assert (key.kind, key.by) == ("secret", By.RULE)
    assert key.reason == "claude-cli model=actual-model 불가: 보수적 secret"
    assert out.source is None


def test_default_client_failure_keeps_actual_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ddak.plan.analyze.logic.get_jev_client",
        lambda: GroqJevClient(None, model="selected-groq-model", timeout_s=2),
    )
    out = _run(tmp_path, {".env.example": "MYSTERY=\n"})
    key = _by_name(out)["MYSTERY"]
    assert (key.provider, key.model) == ("groq", "selected-groq-model")
    assert key.by is By.RULE and out.source is None


@pytest.mark.parametrize("source", [Source.FIXTURE, Source.REPLAY, Source.CACHE, Source.LIVE])
def test_claude_response_source_reaches_analyze(tmp_path: Path, source: Source) -> None:
    class FakeClaude:
        name = "fake"

        def complete(self, req: AIRequest) -> AIResponse:
            return AIResponse(
                text='{"answers": [{"id": "secret.mystery", "probability": 0.2}]}',
                source=source,
            )

    client = ClaudeJevClient(model="fixture-model", provider=FakeClaude())
    out = _run(tmp_path, {".env.example": "MYSTERY=\n"}, jev=client)
    key = _by_name(out)["MYSTERY"]
    assert (key.provider, key.model) == ("claude-cli", "fixture-model")
    assert key.by is By.AI and key.kind == "plain"
    assert out.source is source and client.source is source
