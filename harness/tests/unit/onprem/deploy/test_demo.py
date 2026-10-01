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
            "INSERT INTO env_release VALUES (?, 'cloud','DIVERGED',?,NULL)",
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
    elif guard == "missing":
        rid = "nonexistent"
    elif guard == "foreign":
        fake.containers["app-2"]["Config"]["Labels"]["ddak.project"] = "foreign"
    else:
        with store.connection() as db:
            if guard == "lock":
                db.execute("INSERT INTO locks VALUES (?, ?, 'token', 0, 0)", (project, "rel-v2"))
            elif guard in {"running", "human-run", "failed"}:
                status = {
                    "running": "RUNNING",
                    "human-run": "NEEDS_HUMAN",
                    "failed": "FAILED_LOCAL",
                }[guard]
                db.execute("UPDATE runs SET status=? WHERE run_id='rel-v1'", (status,))
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
    with (state / "controller.lock").open("a") as lock:
        if guard == "flock":
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DdakToolError):
            reset_demo(state, project, ctx.platform["onprem"], rid, runner=fake)
    assert path.read_text() == original_env
    assert ids == {name: s["Id"] for name, s in fake.containers.items()}


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


def test_cli_preflight_inventory_keeps_legacy_signature(tmp_path, monkeypatch, capsys):
    script = load_script("o1_demo")
    path = tmp_path / "inventory.json"
    path.write_text('{"mode":"container"}')
    monkeypatch.setattr(
        script, "preflight_inventory", lambda data, project: {"passed": False, "checks": []}
    )
    assert script.main(["--preflight", "--inventory", str(path)]) == 3
    assert '"passed": false' in capsys.readouterr().out
