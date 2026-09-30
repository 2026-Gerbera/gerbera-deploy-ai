"""CI 약화 방지(G-1 #18): 에이전트가 테스트를 통과시키려고 CI를 좁히는 것을 막는다.

devpi-guardian의 "CI 전체 문자열 비교"는 너무 경직돼 버리고 금지 목록 방식만 쓴다.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

from tests.support import REPO_ROOT

WORKFLOW_ROOT = REPO_ROOT.parent
WORKFLOWS = sorted((WORKFLOW_ROOT / ".github" / "workflows").glob("*.y*ml"))
FORBIDDEN_TEXT = ("continue-on-error", "|| true", "contents: write", "pull_request_target")
PYTEST_NARROWING = re.compile(r"(?:^|\s)(-k|--deselect|--ignore(?:-glob)?|-m)(?:\s|=|$)")
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$", re.MULTILINE)
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _load(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(doc: dict[Any, Any]) -> list[dict[str, Any]]:
    return [step for job in doc["jobs"].values() for step in job.get("steps", [])]


def test_workflows_exist() -> None:
    assert WORKFLOWS, "CI 워크플로가 없다"


def test_no_forbidden_text() -> None:
    for path in WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        for bad in FORBIDDEN_TEXT:
            assert bad not in text, f"{path.name}: {bad!r} 금지"


def test_permissions_are_read_only() -> None:
    for path in WORKFLOWS:
        doc = _load(path)
        assert doc.get("permissions") == {"contents": "read"}, path.name
        for name, job in doc["jobs"].items():
            assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}, name
            assert "timeout-minutes" in job, f"{name}: timeout-minutes 필요"


def test_actions_are_pinned_to_full_sha_with_version_comment() -> None:
    for path in WORKFLOWS:
        for ref, rest in USES.findall(path.read_text(encoding="utf-8")):
            assert PINNED.match(ref), f"{path.name}: SHA 고정 필요: {ref}"
            assert "#" in rest, f"{path.name}: 버전 주석 필요: {ref}"


def test_no_expression_interpolation_in_run() -> None:
    for path in WORKFLOWS:
        for step in _steps(_load(path)):
            assert "${{" not in step.get("run", ""), f"{path.name}: run에 ${{{{ }}}} 금지(env로)"


def test_pytest_is_not_narrowed_in_ci_or_dev_script() -> None:
    for path in WORKFLOWS:
        for step in _steps(_load(path)):
            run = step.get("run", "")
            if "pytest" in run:
                assert not PYTEST_NARROWING.search(run), f"{path.name}: pytest 범위 축소 금지"
    dev = (REPO_ROOT / "scripts" / "dev.py").read_text(encoding="utf-8")
    for flag in ('"--deselect"', '"--ignore"', '"-k"'):
        assert flag not in dev, f"scripts/dev.py: {flag} 금지"


def test_pytest_addopts_only_excludes_manual_markers() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    assert addopts == [
        "-ra",
        "--strict-markers",
        "--import-mode=importlib",
        "-m",
        "not docker and not aws and not llm",
    ]


def test_required_jobs_present() -> None:
    ci = _load(WORKFLOW_ROOT / ".github" / "workflows" / "ci.yml")
    assert set(ci["jobs"]) >= {"quality", "attribution", "secrets"}
    on = ci.get("on", ci.get(True))  # PyYAML은 on:을 True로 읽는다
    assert "pull_request" in on
    assert on["push"]["branches"] == ["main"]


def test_attribution_uses_current_guard_without_email_list() -> None:
    ci = _load(WORKFLOW_ROOT / ".github" / "workflows" / "ci.yml")
    runs = [step.get("run", "") for step in ci["jobs"]["attribution"]["steps"]]
    assert any("git_guard.py range" in run for run in runs)
    assert not any("git checkout" in run or "allowed-authors" in run for run in runs)


def test_ci_targets_nested_harness_and_keeps_secrets_at_repository_root() -> None:
    ci = _load(WORKFLOW_ROOT / ".github/workflows/ci.yml")
    quality = ci["jobs"]["quality"]
    assert quality["defaults"]["run"]["working-directory"] == "harness"
    setup = next(s for s in quality["steps"] if s.get("uses", "").startswith("astral-sh/setup-uv@"))
    assert setup["with"]["python-version-file"] == "harness/.python-version"
    assert setup["with"]["cache-dependency-glob"] == "harness/uv.lock"
    assert all(
        "harness/scripts/git_guard.py" in s["run"]
        for s in ci["jobs"]["attribution"]["steps"]
        if "run" in s
    )
    assert "defaults" not in ci and "defaults" not in ci["jobs"]["secrets"]
