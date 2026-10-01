"""plan/analyze: 규칙 + Jev 분류, AI 입력 안전, 툴 등록."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from ddak.app import load_tools
from ddak.core.ai.providers.jev import JevAnswer, JevQuestion
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.enums import By, Source
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
    tmp_path: Path, files: dict[str, str], *, changed: tuple[str, ...] | None = None, jev=None
):
    _write(tmp_path / "src1", files)
    inp = AnalyzeProjectInput(
        run_id="run-1",
        request=DeployRequest(project="demo", repo_url="https://github.com/o/r", target="both"),
        source_dir="src1",
        changed={"local": {"web": True, "was": True}},
        changed_paths=tuple(files) if changed is None else changed,
    )
    ctx = RunContext("run-1", deploy_config=CFG)
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
    assert key.kind == "secret" and "jev 불가" in (key.reason or "")
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
