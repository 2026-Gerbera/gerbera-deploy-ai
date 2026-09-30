"""fixture 출처가 붙은 service E2E. Docker/클라우드/AI 성공 증명으로 사용하지 않는다."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from unittest.mock import Mock

import pytest

from ddak.core.registry import REGISTRY
from ddak.core.snapshots import digest_json, file_manifest
from ddak.core.store import Store
from ddak.executor.service import DeploymentService
from tests.support import REPO_ROOT, load_script

demo = load_script("o1_demo")


@pytest.mark.parametrize("scenario", tuple(demo.EXPECTED))
def test_three_independent_v2_rounds_verify_gates_artifacts_and_rollback(tmp_path, scenario):
    root = tmp_path / "o1-demo"
    original = file_manifest(demo.FIXTURES / "source")
    registered = REGISTRY.registered()
    run_ids = set()
    for _ in range(3):
        output = []
        summary = asyncio.run(demo.run_fixture(root, scenario, yes=True, emit=output.append))
        assert output[0] == demo.BANNER
        assert summary["expectation_met"], summary
        assert summary["source"] == summary["source_mode"] == "fixture"
        assert summary["live_three_minute_verified"] is False
        assert summary["source_files"] == original
        assert file_manifest(demo.FIXTURES / "source") == original
        assert summary["snapshot"]["source_snapshot_hash"] == digest_json(original)
        assert (
            summary["snapshot"]["source_snapshot_hash"]
            != summary["snapshot"]["build_snapshot_hash"]
        )
        assert all(value >= 0 for value in summary["timing_s"].values())
        assert REGISTRY.registered() == registered
        v1, v2 = summary["runs"]["v1"], summary["runs"]["v2"]
        assert not {v1, v2} & run_ids
        run_ids.update((v1, v2))
        store = Store(root / "ddak.sqlite")
        assert store.run(v1)["status"] == "SUCCEEDED"
        assert store.run(v2)["status"] == demo.EXPECTED[scenario][0]
        approvals = store.approvals(v2)
        assert {record.kind for record in approvals} == {"deploy", "patch"}
        assert len({record.approval_id for record in approvals}) == 1
        assert all(record.decision == "approved" for record in approvals)
        run = root / "runs" / v2
        assert json.loads((run / "build-source" / "app.json").read_text())["version"] == "v2"
        release = json.loads((run / "release.json").read_text())
        assert release["artifacts"]["snapshot"] == summary["snapshot"]
        assert release["source_mode"] == "fake"
        assert set(release["artifacts"]["observations"]) == (
            {"local"} if scenario == "local_fail" else {"local", "cloud"}
        )
        calls = [item for item in summary["calls"] if item["run_id"] == v2]
        rolled_back = [item["target"] for item in calls if item["tool"] == "rollback_tier"]
        assert (
            rolled_back
            == {
                "success": [],
                "local_fail": ["local"],
                "cloud_fail": ["cloud"],
                "parity_fail": ["cloud"],
            }[scenario]
        )
        assert any(item["tool"] == "post_report" for item in calls)
        events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
        assert [event["seq"] for event in events] == list(range(len(events)))
        if scenario == "local_fail":
            assert not any(item["target"] == "cloud" for item in calls)
            assert summary["gates"]["local_verified"] is False
        else:
            gate = next(
                event["seq"]
                for event in events
                if event.get("detail") == "local_verified" and event["type"] == "gate.opened"
            )
            mutation = next(
                event["seq"]
                for event in events
                if event.get("step") == "deploy.db.cloud" and event["type"] == "step.started"
            )
            assert gate < mutation
        if scenario in {"cloud_fail", "parity_fail"}:
            assert set(summary["environment_status"].values()) == {"DIVERGED"}
        demo.reset_fixture(root)
        assert not (root / "ddak.sqlite").exists()
        assert not (root / "runs").exists()
    assert len(run_ids) == 6


@pytest.mark.parametrize("answer, expected", [("n", "DENIED"), ("y", "SUCCEEDED")])
def test_one_interactive_approval_for_both_runs(tmp_path, answer, expected):
    root = tmp_path / "o1-demo"
    ask = Mock(return_value=answer)
    summary = asyncio.run(demo.run_fixture(root, "success", ask=ask, emit=lambda _: None))
    ask.assert_called_once()
    assert summary["status"] == expected
    if answer == "n":
        assert not list((root / "runs").glob("*/build-source"))
        assert Store(root / "ddak.sqlite").environments(demo.PROJECT) == {}


def test_cli_applies_patch_inside_checkout_and_preserves_existing_runs(
    tmp_path, monkeypatch, capsys
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "--quiet", str(checkout)], check=True, capture_output=True)
    root = checkout / "var" / "o1-demo"
    monkeypatch.setattr(demo, "STATE", root)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path / "other"))
    assert demo.main(["--yes"]) == 0
    assert os.environ["GIT_CEILING_DIRECTORIES"] == str(tmp_path / "other")
    before = (root / "summary.json").read_bytes()
    assert json.loads(before)["expectation_met"]
    assert demo.main(["--yes"]) == 1
    assert (root / "summary.json").read_bytes() == before
    assert "기존 실행 기록" in capsys.readouterr().err


def test_cli_real_yes_rejected_and_real_missing_stays_dry_run(monkeypatch, capsys):
    monkeypatch.setattr(demo, "run_fixture", Mock(side_effect=AssertionError("fake fallback")))
    monkeypatch.setattr(demo, "preflight", lambda: {"docker": {"status": "missing"}})
    with pytest.raises(SystemExit) as exc:
        demo.main(["--mode", "real", "--yes"])
    assert exc.value.code == 2
    assert demo.main(["--mode", "real"]) == 3
    assert demo.main(["--mode", "local"]) == 3
    assert "dry-run only" in capsys.readouterr().out


def test_reset_rejects_active_controller_unknown_files_and_symlinks(tmp_path):
    root = tmp_path / "o1-demo"
    demo.initialize(root)
    service = DeploymentService(demo.FixtureRuntime("success").registry, root)
    try:
        with pytest.raises(ValueError, match="활성 컨트롤러"):
            demo.reset_fixture(root)
    finally:
        service.close()
    user_file = root / "user-file"
    user_file.write_text("keep")
    with pytest.raises(ValueError, match="알 수 없는"):
        demo.reset_fixture(root)
    assert user_file.read_text() == "keep"
    user_file.unlink()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (root / "runs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="링크"):
        demo.reset_fixture(root)
    assert (outside / "keep").read_text() == "keep"


@pytest.mark.parametrize("status", ["RUNNING", "NEEDS_HUMAN"])
def test_reset_preserves_unresolved_state(tmp_path, status):
    root = tmp_path / "o1-demo"
    demo.initialize(root)
    store = Store(root / "ddak.sqlite")
    store.create_run("unfinished", demo.PROJECT, "hash")
    with store.connection() as db:
        db.execute("UPDATE runs SET status=?", (status,))
    with pytest.raises(ValueError, match="수동 확인"):
        demo.reset_fixture(root)
    assert store.run("unfinished")["status"] == status


def test_reset_refuses_unknown_root(tmp_path):
    root = tmp_path / "o1-demo"
    root.mkdir()
    (root / "important").write_text("keep")
    with pytest.raises(ValueError, match="소유"):
        demo.reset_fixture(root)
    with pytest.raises(ValueError, match="전용"):
        demo.reset_fixture(tmp_path)


def test_container_reset_filters_and_rechecks_both_labels(tmp_path, monkeypatch):
    root = tmp_path / "o1-demo"
    demo.initialize(root)
    monkeypatch.setattr(
        demo, "docker_preflight", lambda: {"status": "ready", "endpoint": "unix:///fixture.sock"}
    )
    calls = []
    labels = {"ddak.demo": "true", "ddak.project": "someone-else"}

    def docker(args, *, timeout=5):
        calls.append(args)
        if "ps" in args:
            return subprocess.CompletedProcess(args, 0, "a" * 12 + "\n", "")
        if "inspect" in args:
            return subprocess.CompletedProcess(args, 0, json.dumps(labels), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(demo, "_docker", docker)
    with pytest.raises(ValueError, match="소유 라벨"):
        demo.reset_fixture(root, containers=True)
    assert not any("rm" in args for args in calls)
    assert "label=ddak.demo=true" in calls[0]
    assert f"label=ddak.project={demo.PROJECT}" in calls[0]
    labels["ddak.project"] = demo.PROJECT
    result = demo.reset_fixture(root, containers=True)
    assert result["containers_removed"] == 1
    assert [args for args in calls if "rm" in args] == [
        ["--host", "unix:///fixture.sock", "rm", "-f", "a" * 12]
    ]


def test_preflight_bounds_docker_info_and_reports_timeouts(monkeypatch):
    calls = []
    monkeypatch.setenv("DOCKER_HOST", "unix:///fixture.sock")

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(demo.subprocess, "run", run)
    assert demo.docker_preflight() == {"status": "timeout"}
    assert calls[0][0] == [
        "docker",
        "--host",
        "unix:///fixture.sock",
        "info",
        "--format",
        "{{.ServerVersion}}",
    ]
    assert calls[0][1]["timeout"] == 5
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.invalid:2375")
    assert demo.docker_preflight()["status"] == "unsupported_remote"
    assert len(calls) == 1


def test_preflight_distinguishes_internal_work_from_registry_missing(monkeypatch, capsys):
    from ddak import app
    from ddak.core.registry import CATALOG, Registry

    registry = Registry(CATALOG)
    original_specs = [spec.model_dump() for spec in registry.specs]
    original_missing = sorted(registry.missing())
    monkeypatch.setattr(app, "load_tools", lambda: registry)
    monkeypatch.setattr(demo, "docker_preflight", lambda: {"status": "ready"})
    report = demo.preflight()
    assert report["implemented_internal"] == {
        "acquire_deploy_lock": "Store.acquire + service",
        "record_deploy_log": "Store.finish",
        "preflight_check": "local CLI only",
        "reset_demo_state": "fixture only",
    }
    assert report["registry_missing"] == original_missing
    assert set(report["missing"]) == set(original_missing) - report["implemented_internal"].keys()
    assert set(report["missing_owners"]) == set(report["missing"])
    assert "build_image" in report["missing"]
    assert report["cloud_checked"] is False
    assert "클라우드 운영 기능의 준비 완료를 뜻하지 않는다" in report["missing_scope"]
    assert report["real_pipeline"].startswith("dry-run only")
    assert [spec.model_dump() for spec in registry.specs] == original_specs
    assert registry.registered() == frozenset()
    assert demo.main(["--preflight"]) == 3
    assert (
        json.loads(capsys.readouterr().out)["implemented_internal"]
        == report["implemented_internal"]
    )


def test_dev_routes_demo_local_preflight_and_reset_without_global_clean(monkeypatch):
    dev = load_script("dev")
    runner = Mock(return_value=0)
    monkeypatch.setattr(dev, "run", runner)
    assert dev.main(["demo", "--scenario", "parity_fail", "--yes"]) == 0
    runner.assert_called_with(
        [
            sys.executable,
            "scripts/o1_demo.py",
            "--mode",
            "fixture",
            "--scenario",
            "parity_fail",
            "--yes",
        ]
    )
    assert dev.main(["demo-local"]) == 0
    runner.assert_called_with([sys.executable, "scripts/o1_demo.py", "--mode", "local"])
    assert dev.main(["preflight"]) == 0
    runner.assert_called_with([sys.executable, "scripts/o1_demo.py", "--preflight"])
    assert dev.main(["demo-reset"]) == 0
    runner.assert_called_with([sys.executable, "scripts/o1_demo.py", "--reset"])
    before = runner.call_count
    assert dev.main(["demo-reset", "--cloud"]) == 3
    assert runner.call_count == before


def test_clean_preserves_active_runtime_database_lock_and_build_source(
    tmp_path, monkeypatch, capsys
):
    dev = load_script("dev")
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    state = tmp_path / "var" / "o1-demo"
    demo.initialize(state)
    service = DeploymentService(demo.FixtureRuntime("success").registry, state)
    try:
        service.store.create_run("active", demo.PROJECT, "hash")
        runtime_cache = state / "runs" / "active" / "build-source" / "__pycache__"
        runtime_cache.mkdir(parents=True)
        sentinel = runtime_cache / "keep"
        sentinel.write_text("runtime")
        before = (state / "ddak.sqlite").read_bytes()
        inode = (state / "controller.lock").stat().st_ino
        for directory in (".pytest_cache", ".ruff_cache", "scripts/__pycache__"):
            cache = tmp_path / directory
            cache.mkdir(parents=True)
            (cache / "remove").write_text("cache")
        assert dev.main(["clean"]) == 0
        assert (state / "ddak.sqlite").read_bytes() == before
        assert (state / "controller.lock").stat().st_ino == inode
        assert sentinel.read_text() == "runtime"
        assert not (tmp_path / ".pytest_cache").exists()
        assert not (tmp_path / ".ruff_cache").exists()
        assert not (tmp_path / "scripts" / "__pycache__").exists()
        assert "런타임 상태·DB·잠금은 보존" in capsys.readouterr().out
        with pytest.raises(ValueError, match="활성 컨트롤러"):
            demo.reset_fixture(state)
    finally:
        service.close()


def test_local_mode_routes_only_real_docker_tests(monkeypatch, capsys):
    monkeypatch.setattr(demo, "preflight", lambda: {"docker": {"status": "ready"}})
    runner = Mock(return_value=subprocess.CompletedProcess([], 17))
    monkeypatch.setattr(demo.subprocess, "run", runner)
    # 런타임 담당자의 테스트가 있는 경우만 실제 경로로 연결한다.
    if not list((REPO_ROOT / "tests" / "docker").glob("test_*.py")):
        pytest.skip("실제 Docker 테스트 연결 대기")
    assert demo.main(["--mode", "local"]) == 17
    assert "provider 통합 테스트" in capsys.readouterr().out
    assert runner.call_args.args[0][-3:] == ["-m", "docker", str(REPO_ROOT / "tests" / "docker")]
    assert runner.call_args.kwargs["env"]["DDAK_TEST_DOCKER"] == "1"
