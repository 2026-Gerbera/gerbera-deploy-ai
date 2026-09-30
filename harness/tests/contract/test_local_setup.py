"""설치 명령의 부작용 경계. 실제 uv 설치·Git 설정·사용자 Claude 파일 접근은 없다."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

from tests.support import REPO_ROOT, load_script


@pytest.fixture
def dev(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    module = load_script("dev")
    root = tmp_path / "template"
    root.mkdir()
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "run", Mock(return_value=0))
    monkeypatch.setattr(module, "capture", Mock(return_value=(0, "uv 0.12.20")))
    monkeypatch.setattr(
        module, "_claude_attribution_ok", Mock(side_effect=AssertionError("Claude 설정 접근 금지"))
    )
    return module


def _setup_local(dev: ModuleType):
    task = getattr(dev, "task_setup_local", None)
    assert callable(task), "비Git 템플릿용 task_setup_local(no_sync=False)이 필요하다"
    return task


def test_setup_local_only_syncs_uv_and_preserves_existing_lock(dev: ModuleType) -> None:
    task = _setup_local(dev)
    assert not (dev.ROOT / ".git").exists()
    assert task() == 0
    dev.run.assert_called_once_with(["uv", "sync"])
    dev.capture.assert_called_once_with(["uv", "--version"])
    dev.run.reset_mock()
    dev.capture.reset_mock()
    (dev.ROOT / "uv.lock").write_text("test lock sentinel\n", encoding="utf-8")
    assert task() == 0
    dev.run.assert_called_once_with(["uv", "sync", "--locked"])
    dev.capture.assert_called_once_with(["uv", "--version"])
    assert (dev.ROOT / "uv.lock").read_text(encoding="utf-8") == "test lock sentinel\n"
    dev._claude_attribution_ok.assert_not_called()
    assert not (dev.ROOT / ".git").exists()
    assert not (dev.ROOT / ".claude").exists()


def test_setup_local_no_sync_skips_installation(dev: ModuleType) -> None:
    assert _setup_local(dev)(no_sync=True) == 0
    dev.run.assert_not_called()
    assert all(call.args[0] == ["uv", "--version"] for call in dev.capture.call_args_list)
    dev._claude_attribution_ok.assert_not_called()


def test_setup_local_reports_missing_uv_bad_version_and_sync_failure(dev: ModuleType) -> None:
    task = _setup_local(dev)
    for result in ((127, ""), (0, "uv 0.12.19"), (0, "uv 0.13.0")):
        dev.capture.return_value = result
        assert task() != 0
        dev.run.assert_not_called()
    dev.capture.return_value = (0, "uv 0.12.20")
    dev.run.return_value = 17
    assert task() != 0
    dev.run.assert_called_once_with(["uv", "sync"])
    dev._claude_attribution_ok.assert_not_called()


def test_setup_local_cli_and_makefile_route_to_local_setup(
    dev: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_local(dev)
    calls = []

    def setup_local(no_sync: bool = False) -> int:
        calls.append(no_sync)
        return 17

    monkeypatch.setattr(dev, "task_setup_local", setup_local)
    assert dev.main(["setup-local"]) == 17
    assert dev.main(["setup-local", "--no-sync"]) == 17
    assert calls == [False, True]
    # 같은 이름의 파일이 있어도 .PHONY 진입점으로 실행되는지 확인한다.
    (tmp_path / "setup-local").touch()
    result = subprocess.run(
        ["make", "--dry-run", "-f", str(REPO_ROOT / "Makefile"), "setup-local"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "python3 scripts/dev.py setup-local" in result.stdout
    assert "uv run" not in result.stdout


@pytest.mark.parametrize("git_root", ["missing", "parent", "own", "harness"])
def test_full_setup_accepts_repository_or_its_harness_only(
    dev: ModuleType, git_root: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if git_root == "harness":
        nested = dev.ROOT.parent / "harness"
        nested.mkdir()
        monkeypatch.setattr(dev, "ROOT", nested)
    hook_dir = dev.ROOT / ".githooks"
    hook_dir.mkdir()
    hook = hook_dir / "pre-commit"
    hook.write_text("test hook sentinel\n", encoding="utf-8")
    hook.chmod(0o644)
    before_mode = hook.stat().st_mode
    parent_config = dev.ROOT.parent / "parent-config-sentinel"
    parent_config.write_text("unchanged\n", encoding="utf-8")
    roots = {
        "missing": (128, ""),
        "parent": (0, str(dev.ROOT.parent)),
        "own": (0, str(dev.ROOT)),
        "harness": (0, str(dev.ROOT.parent)),
    }

    def capture(cmd: list[str]) -> tuple[int, str]:
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return roots[git_root]
        if git_root not in ("own", "harness"):
            pytest.fail(f"Git 루트 불일치 뒤 후속 조회가 실행됐다: {cmd}")
        if cmd == ["uv", "--version"]:
            return 0, "uv 0.12.20"
        if cmd == ["git", "config", "--get", "user.name"]:
            return 0, "Test Operator"
        if cmd == ["git", "config", "--get", "user.email"]:
            return 0, "operator@example.invalid"
        pytest.fail(f"예상하지 않은 조회: {cmd}")

    monkeypatch.setattr(dev, "capture", capture)
    if git_root in ("own", "harness"):
        monkeypatch.setattr(dev, "_claude_attribution_ok", Mock(return_value=None))
        assert dev.task_setup(no_sync=True) == 0
        assert dev.run.call_count == 2
        expected = "harness/.githooks" if git_root == "harness" else ".githooks"
        dev.run.assert_any_call(["git", "config", "--local", "core.hooksPath", expected])
    else:
        assert dev.task_setup(no_sync=True) != 0
        dev.run.assert_not_called()
        dev._claude_attribution_ok.assert_not_called()
        assert hook.stat().st_mode == before_mode
    assert parent_config.read_text(encoding="utf-8") == "unchanged\n"
