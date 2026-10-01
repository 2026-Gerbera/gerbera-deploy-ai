"""source=fake: 보호된 real-mode reset의 가드·장부·복구 순서 검증."""

from __future__ import annotations

import fcntl
import json
from dataclasses import replace
from pathlib import Path

import pytest

from ddak.core.contracts.errors import DdakToolError
from ddak.core.store import Store
from ddak.onprem.deploy import reset_demo
from tests.support import load_script
from tests.unit.onprem.deploy.test_onprem_vm import vm as vm


def _record(store, project, rid, ref, *, source_mode="real", local_status="SUCCEEDED"):
    store.create_run(rid, project, "sha256:" + "a" * 64)
    release = {
        "release_id": rid,
        "source_mode": source_mode,
        "source": {},
        "files": {},
        "source_files": {},
        "images": {"was": ref},
        "artifacts": None,
    }
    result = {"status": "SUCCEEDED", "tracks": {"local": "DONE"}, "steps": {}}
    store.finish(
        rid, "SUCCEEDED", result, {**release, "result": result}, {"local": (local_status, release)}
    )
    return release


@pytest.fixture
def demo(vm, tmp_path, monkeypatch):
    fake, provider, ctx = vm
    state = tmp_path / "state"
    store = Store(state / "ddak.sqlite")
    ctx = replace(ctx, run_id="rel-v1")
    ctx.platform["onprem"]["tiers"]["was"]["replicas"] = 3
    provider.deploy("was", ctx)
    v1 = _record(store, ctx.project, "rel-v1", fake.refs[0])
    provider.inject_config(["SECRET_KEY"], ctx)
    updated = replace(
        ctx, run_id="rel-v2", images={"was": fake.refs[1]}, previous_release={"local": v1}
    )
    provider.deploy("was", updated)
    _record(store, ctx.project, "rel-v2", fake.refs[1], local_status="DIVERGED")
    with store.connection() as db:
        db.execute(
            "INSERT INTO env_release(project,target,status,current,previous) "
            "VALUES (?, 'cloud','DIVERGED',?,NULL)",
            (ctx.project, json.dumps(v1)),
        )
    monkeypatch.setenv("ALLOW_DEMO_RESET", "1")
    return fake, provider, ctx, store, state


def test_reset_removes_secret_before_recreate_updates_ledger_and_repeats(demo):
    fake, _, ctx, store, state = demo
    inv = ctx.platform["onprem"]
    path = Path(inv["tiers"]["was"]["env_file"])
    with path.open("a") as stream:
        stream.write("# preserve\nOTHER_KEY=fixture-value\n")
    cloud = store.environments(ctx.project)["cloud"]["current"]
    original = fake.__call__

    def runner(argv, *, timeout):
        if "create" in argv:
            assert "SECRET_KEY=" not in path.read_text()
        return original(argv, timeout=timeout)

    first = reset_demo(state, ctx.project, inv, "rel-v1", runner=runner)
    assert first["status"] == "SUCCEEDED" and first["secret_key_removed"]
    assert first["replicas"]["was"]["detail"] == "복구 replica: [1, 2, 3]"
    assert path.read_text() == "# preserve\nOTHER_KEY=fixture-value\n"
    env = store.environments(ctx.project)
    assert env["local"]["current"]["release_id"] == "rel-v1"
    assert env["local"]["previous"]["release_id"] == "rel-v2"
    assert env["local"]["status"] == "SUCCEEDED"
    assert env["cloud"]["current"] == cloud and env["cloud"]["status"] == "ROLLED_BACK"
    second = reset_demo(state, ctx.project, inv, "rel-v1", runner=runner)
    assert not second["replicas"]["was"]["changed"] and not second["secret_key_removed"]
    assert store.environments(ctx.project) == env
    assert len(list((state / "ops").glob("reset-*.json"))) == 2
    assert store.run("rel-v2")["status"] == "SUCCEEDED"  # 과거 run은 고치지 않는다.


@pytest.mark.parametrize(
    "guard",
    [
        "allow",
        "project",
        "lock",
        "running",
        "human-run",
        "human-env",
        "flock",
        "fake",
        "failed",
        "missing",
        "foreign",
    ],
)
def test_reset_guards_preserve_runtime_and_secret(demo, guard, monkeypatch):
    fake, _, ctx, store, state = demo
    project = ctx.project
    rid = "rel-v1"
    if guard == "allow":
        monkeypatch.delenv("ALLOW_DEMO_RESET")
    elif guard == "project":
        project = "production"
        rid = "production-v1"
        _record(store, project, rid, fake.refs[0])
    elif guard == "missing":
        rid = "nonexistent"
    elif guard == "foreign":
        fake.containers["app-2"]["Config"]["Labels"]["ddak.project"] = "foreign"
    else:
        with store.connection() as db:
            if guard == "lock":
                db.execute(
                    "INSERT INTO locks(project,run_id,token,heartbeat,expires) "
                    "VALUES (?, ?, 'token', 0, 0)",
                    (project, "rel-v2"),
                )
            elif guard in {"running", "human-run", "failed"}:
                status = {
                    "running": "RUNNING",
                    "human-run": "NEEDS_HUMAN",
                    "failed": "FAILED_LOCAL",
                }[guard]
                db.execute(
                    "UPDATE runs SET status=? WHERE run_id=?",
                    (status, "rel-v1" if guard == "failed" else "rel-v2"),
                )
            elif guard == "human-env":
                db.execute("UPDATE env_release SET status='NEEDS_HUMAN'")
            elif guard == "fake":
                row = db.execute("SELECT manifest FROM releases WHERE run_id='rel-v1'").fetchone()
                manifest = json.loads(row[0])
                manifest["source_mode"] = "fake"
                db.execute(
                    "UPDATE releases SET manifest=? WHERE run_id='rel-v1'", (json.dumps(manifest),)
                )
    path = Path(ctx.platform["onprem"]["tiers"]["was"]["env_file"])
    original_env = path.read_text()
    ids = {name: s["Id"] for name, s in fake.containers.items()}
    before = store.environments(project)
    messages = {
        "allow": "allowlist",
        "project": "allowlist",
        "lock": "RUNNING/NEEDS_HUMAN/잠금",
        "running": "RUNNING/NEEDS_HUMAN/잠금",
        "human-run": "RUNNING/NEEDS_HUMAN/잠금",
        "human-env": "RUNNING/NEEDS_HUMAN/잠금",
        "flock": "활성 컨트롤러",
        "fake": "REAL local 성공",
        "failed": "성공한 기준 release",
        "missing": "성공한 기준 release",
        "foreign": "소유 라벨",
    }
    with (state / "controller.lock").open("a") as lock:
        if guard == "flock":
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DdakToolError, match=messages[guard]):
            reset_demo(state, project, ctx.platform["onprem"], rid, runner=fake)
    assert path.read_text() == original_env
    assert ids == {name: s["Id"] for name, s in fake.containers.items()}
    assert store.environments(project) == before
    assert not list((state / "ops").glob("*.json"))


def test_reset_failure_keeps_release_and_marks_human(demo):
    fake, _, ctx, store, state = demo
    before = store.environments(ctx.project)["local"]["current"]
    fake.fail_prefix = ["container", "create"]
    with pytest.raises(DdakToolError, match="수동 확인"):
        reset_demo(state, ctx.project, ctx.platform["onprem"], "rel-v1", runner=fake)
    after = store.environments(ctx.project)["local"]
    assert after["current"] == before and after["status"] == "NEEDS_HUMAN"
    audit = json.loads(next((state / "ops").glob("*.json")).read_text())
    assert audit["status"] == "NEEDS_HUMAN" and "private" not in str(audit)
    with pytest.raises(DdakToolError, match="수동 확인"):
        reset_demo(state, ctx.project, ctx.platform["onprem"], "rel-v1", runner=fake)


@pytest.mark.parametrize("passed", [True, False])
def test_cli_preflight_inventory_exit_code(tmp_path, monkeypatch, capsys, passed):
    script = load_script("o1_demo")
    path = tmp_path / "inventory.json"
    path.write_text('{"mode":"container"}')
    monkeypatch.setattr(
        script, "preflight_inventory", lambda data, project: {"passed": passed, "checks": []}
    )
    assert script.main(["--preflight", "--inventory", str(path)]) == (0 if passed else 3)
    assert json.loads(capsys.readouterr().out)["passed"] == passed


def test_reset_preserves_successful_cloud_row(demo):
    fake, _, ctx, store, state = demo
    with store.connection() as db:
        db.execute("UPDATE env_release SET status='SUCCEEDED' WHERE target='cloud'")
    before = store.environments(ctx.project)["cloud"]
    reset_demo(state, ctx.project, ctx.platform["onprem"], "rel-v1", runner=fake)
    assert store.environments(ctx.project)["cloud"] == before


def test_reset_recreates_v1_with_old_env_keys_only_in_reset(demo):
    fake, provider, ctx, store, state = demo
    provider.rollback(
        "was",
        replace(
            ctx, previous_release={"local": store.environments(ctx.project)["local"]["previous"]}
        ),
    )
    ids = {n: s["Id"] for n, s in fake.containers.items()}
    report = reset_demo(state, ctx.project, ctx.platform["onprem"], "rel-v1", runner=fake)
    assert report["replicas"]["was"]["detail"] == "복구 replica: [1, 2, 3]"
    assert all(s["Id"] != ids[n] for n, s in fake.containers.items())


def test_reset_inventory_error_does_not_expose_input(demo, capsys):
    fake, _, ctx, _, state = demo
    invalid = {**ctx.platform["onprem"], "password": "fixture-" + "private-marker"}
    with pytest.raises(DdakToolError, match="onprem 인벤토리 오류") as caught:
        reset_demo(state, ctx.project, invalid, "rel-v1", runner=fake)
    assert "private-marker" not in str(caught.value)
    assert "input_value" not in str(caught.value)


def test_reset_final_audit_failure_preserves_committed_success(demo, monkeypatch):
    import ddak.onprem.deploy.demo as module

    fake, _, ctx, store, state = demo
    original = module._audit

    def audit(path, report):
        if report["status"] == "SUCCEEDED":
            raise OSError("fixture audit failure")
        original(path, report)

    monkeypatch.setattr(module, "_audit", audit)
    result = reset_demo(state, ctx.project, ctx.platform["onprem"], "rel-v1", runner=fake)
    assert result["status"] == "SUCCEEDED" and "감사 기록 저장 실패" in result["warning"]
    assert store.environments(ctx.project)["local"]["status"] == "SUCCEEDED"


def test_reset_readonly_failure_without_mutation_keeps_ledger(demo):
    from ddak.onprem.deploy.config import remove_demo_secret

    fake, _, ctx, store, state = demo
    remove_demo_secret(Path(ctx.platform["onprem"]["tiers"]["was"]["env_file"]))
    before = store.environments(ctx.project)
    fake.fail_prefix = ["image", "pull"]
    with pytest.raises(DdakToolError):
        reset_demo(state, ctx.project, ctx.platform["onprem"], "rel-v1", runner=fake)
    assert store.environments(ctx.project) == before
    assert (
        json.loads(next((state / "ops").glob("*.json")).read_text())["status"]
        == "FAILED_BEFORE_CHANGE"
    )


def test_reset_env_write_failure_keeps_human_guard(demo, monkeypatch):
    import ddak.onprem.deploy.demo as module

    fake, _, ctx, store, state = demo
    before = store.environments(ctx.project)["local"]["current"]

    def incomplete_write(path):
        path.write_text("# incomplete fixture write\n")
        raise OSError("fixture I/O failure")

    monkeypatch.setattr(module, "remove_demo_secret", incomplete_write)
    with pytest.raises(DdakToolError, match="수동 확인"):
        reset_demo(state, ctx.project, ctx.platform["onprem"], "rel-v1", runner=fake)
    after = store.environments(ctx.project)["local"]
    assert after["current"] == before and after["status"] == "NEEDS_HUMAN"


def test_real_reset_cli_required_args_and_forwarding(tmp_path, monkeypatch):
    script = load_script("o1_demo")
    inventory = tmp_path / "inventory.json"
    inventory.write_text('{"mode":"container"}')
    with pytest.raises(SystemExit) as error:
        script.main(["--reset", "--mode", "real", "--inventory", str(inventory)])
    assert error.value.code == 2
    calls = []
    monkeypatch.setenv("DDAK_RUN_DIR", str(tmp_path / "state" / "runs"))

    def reset(*args):
        calls.append(args)
        return {"status": "SUCCEEDED"}

    monkeypatch.setattr(script, "reset_demo", reset)
    assert (
        script.main(
            ["--reset", "--mode", "real", "--inventory", str(inventory), "--release", "rel-v1"]
        )
        == 0
    )
    assert calls == [(tmp_path / "state", "flaskr", {"mode": "container"}, "rel-v1")]
