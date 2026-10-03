"""source=fake: manager의 실제 CLI adapter 코드에 fake runner를 연결한다."""

from __future__ import annotations

import copy
import json
import re
import subprocess
from dataclasses import replace

import pytest

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.errors import DdakToolError
from ddak.onprem.deploy import OnPremPreparationManager
from tests.unit.onprem.deploy.test_preparation_fix11 import ACCOUNTS, FakeDatabase


@pytest.fixture(autouse=True)
def no_real_commands(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("실제 프로세스 금지")

    monkeypatch.setattr(subprocess, "Popen", forbidden)


class FakeSetupDocker:
    def __init__(self):
        self.db = FakeDatabase()
        self.calls = []
        self.stdin_calls = []
        self.fail = None
        self.corrupt_new = False
        self.containers = {}
        for tier in ("db", "was", "web"):
            mounts = [
                {
                    "Type": "volume",
                    "Name": tier + "-data",
                    "Destination": "/var/lib/mysql" if tier == "db" else "/data",
                    "RW": True,
                }
            ]
            self.containers[tier] = {
                "Id": "old-" + tier,
                "Image": "sha256:" + "a" * 64,
                "Name": "/" + tier,
                "Config": {
                    "Image": "mysql@sha256:" + "b" * 64,
                    "Labels": {
                        "ddak.managed": "true",
                        "ddak.project": "fixture" if tier == "db" else "old",
                        "ddak.tier": tier,
                        "keep": "label",
                    },
                    "Cmd": ["serve"],
                    "Entrypoint": ["entry"],
                    "User": "1000",
                    "WorkingDir": "/work",
                    "Healthcheck": None,
                },
                "HostConfig": {
                    "NetworkMode": "fixture-net",
                    "PortBindings": {},
                    "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                    "Privileged": False,
                    "Binds": None,
                    "CapAdd": None,
                    "Devices": [],
                    "SecurityOpt": None,
                    "ReadonlyRootfs": False,
                },
                "Networks": {"fixture-net": {"Aliases": [tier, "old-" + tier]}},
                "Mounts": mounts,
                "State": {"Running": True},
            }

    def find(self, identifier):
        return next(
            value
            for name, value in self.containers.items()
            if name == identifier or value["Id"] == identifier
        )

    def __call__(self, argv, *, timeout):
        assert argv[:3] == ["docker", "--host", "unix:///fixture.sock"]
        args = argv[3:]
        self.calls.append(args)
        if self.fail and args[: len(self.fail)] == self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "private-error-do-not-show")
        out = ""
        if args[0] == "info":
            out = "fixture-daemon"
        elif args[:2] == ["container", "ls"]:
            pattern = args[-1].removeprefix("name=")
            out = "\n".join(
                value["Id"]
                for name, value in self.containers.items()
                if re.fullmatch(pattern, "/" + name)
            )
        elif args[:2] == ["container", "inspect"]:
            assert ".Config.Env" not in args[-2]
            out = json.dumps(self.find(args[-1]))
        elif args[:2] == ["container", "exec"]:
            sql = args[-1]
            if sql.startswith("SELECT 'database'"):
                state = self.db.state
                rows = ["database\t" + db for db in state.databases]
                rows += [f"table\t{t.database}\t{t.name}\t{int(t.movable)}" for t in state.tables]
                rows += ["account\t" + name for name in state.accounts]
                rows += [
                    f"grant\t'{name}'@'%'\t{db}\t{p}"
                    for name, db, privileges in state.grants
                    for p in privileges
                ]
                out = "\n".join(rows)
            else:
                self.db.execute(sql)
        elif args[:2] == ["volume", "inspect"]:
            out = json.dumps({"Driver": "local", "Options": None})
        elif args[:2] == ["container", "create"]:
            name = args[args.index("--name") + 1]
            tier = "was" if name.startswith("was") else "web"
            state = copy.deepcopy(self.containers[tier])
            state.update(Id="new-" + tier, Name="/" + name)
            labels = dict(
                args[i + 1].split("=", 1) for i, value in enumerate(args) if value == "--label"
            )
            state["Config"]["Labels"] = labels
            state["State"]["Running"] = False
            if self.corrupt_new:
                state["HostConfig"]["NetworkMode"] = "wrong-network"
            self.containers[name] = state
            out = state["Id"]
        elif args[:2] == ["container", "stop"]:
            self.find(args[-1])["State"]["Running"] = False
        elif args[:2] == ["container", "start"]:
            self.find(args[-1])["State"]["Running"] = True
        elif args[:2] == ["container", "rename"]:
            state = self.find(args[-2])
            key = next(name for name, value in self.containers.items() if value is state)
            self.containers[args[-1]] = self.containers.pop(key)
            state["Name"] = "/" + args[-1]
        else:
            pytest.fail("예상하지 못한 adapter 명령: " + repr(args))
        return subprocess.CompletedProcess(argv, 0, out, "")

    def stdin(self, argv, *, input, timeout):
        assert "-i" in argv and input not in repr(argv)
        match = re.search(r"CREATE USER '([A-Za-z_]+)'@'%' IDENTIFIED BY", input)
        assert match and "SET SESSION sql_mode='';" in input
        self.stdin_calls.append((argv, input))
        if self.fail == ["stdin"]:
            return subprocess.CompletedProcess(argv, 1, "", input)
        name = match[1]
        self.db.create_account(
            name, password_env_ref="APP_PASSWORD" if name == "app_user" else "MIGRATOR_PASSWORD"
        )
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def setup(tmp_path):
    fake = FakeSetupDocker()
    env = tmp_path / "runtime.env"
    env.write_text("")
    env.chmod(0o600)
    inventory = {
        "docker_host": "unix:///fixture.sock",
        "tiers": {
            tier: {
                "name": tier,
                "kind": "mysql" if tier == "db" else "python_http",
                "platform": "linux/arm64",
                "network": "fixture-net",
                "env_file": str(env),
                "volumes": [
                    {
                        "name": tier + "-data",
                        "target": "/var/lib/mysql" if tier == "db" else "/data",
                    }
                ],
            }
            for tier in ("db", "was", "web")
        },
    }
    ctx = RunContext(
        "setup-fixture", project="fixture", mode=RunMode.BOOTSTRAP, platform={"onprem": inventory}
    )
    manager = OnPremPreparationManager(tmp_path, ctx, runner=fake, stdin_runner=fake.stdin)
    return manager, fake, ctx


def test_manager_database_runs_real_adapter_with_fake_transport_and_stdin_only(
    setup, monkeypatch, caplog
):
    manager, fake, _ = setup
    plan = manager.plan_database(database="appdb", backup_database="backupdb", accounts=ACCOUNTS)
    assert not fake.stdin_calls and not fake.db.calls
    monkeypatch.setenv("APP_PASSWORD", "only-in-stdin-'\\-fixture")
    monkeypatch.setenv("MIGRATOR_PASSWORD", "migration-only-in-stdin")
    old_db = copy.deepcopy(fake.containers["db"])
    result = manager.apply_database(
        plan, approved_sha=plan.approval_sha, approval_check=lambda sha, summary: True
    )
    assert result.applied == plan.actions
    assert len(fake.stdin_calls) == 2
    assert "only-in-stdin" not in repr(fake.calls)
    assert "only-in-stdin" not in repr(result) + caplog.text
    assert fake.containers["db"] == old_db
    assert not any(
        call[:2] in [["volume", "rm"], ["container", "rm"], ["container", "create"]]
        for call in fake.calls
    )
    assert not caplog.records


@pytest.mark.parametrize("failure", ["sha", "denied", "changed-table", "foreign-db", "bad-volume"])
def test_manager_db_rejects_before_sql_mutation(setup, failure):
    manager, fake, _ = setup
    plan = manager.plan_database(database="appdb", backup_database="backupdb", accounts=ACCOUNTS)
    if failure == "changed-table":
        fake.db.state = replace(fake.db.state, tables=())
    elif failure == "foreign-db":
        fake.containers["db"]["Config"]["Labels"]["ddak.project"] = "old-owner"
    elif failure == "bad-volume":
        fake.containers["db"]["Mounts"][0]["Name"] = "other-data"
    with pytest.raises(DdakToolError):
        manager.apply_database(
            plan,
            approved_sha="0" * 64 if failure == "sha" else plan.approval_sha,
            approval_check=lambda sha, summary: failure != "denied",
        )
    assert not fake.db.calls and not fake.stdin_calls


def test_db_stdin_failure_redacts_secrets_preserves_partial_state(setup, monkeypatch):
    manager, fake, _ = setup
    plan = manager.plan_database(database="appdb", backup_database="backupdb", accounts=ACCOUNTS)
    monkeypatch.setenv("APP_PASSWORD", "sensitive-payload")
    fake.fail = ["stdin"]
    with pytest.raises(DdakToolError) as caught:
        manager.apply_database(
            plan, approved_sha=plan.approval_sha, approval_check=lambda sha, summary: True
        )
    assert "sensitive-payload" not in str(caught.value)
    assert caught.value.needs_human is True
    assert all(t.database == "backupdb" for t in fake.db.state.tables)
    assert manager.routed.payload is None
    assert "db" in fake.containers
    assert "DROP" not in repr(fake.db.calls)


@pytest.mark.parametrize("tier", ["was", "web"])
def test_manager_owner_transfer_replaces_labels_and_keeps_old_container_and_volume(setup, tier):
    manager, fake, _ = setup
    plan = manager.plan_ownership(tier)
    before = copy.deepcopy(fake.containers[tier])
    result = manager.apply_ownership(
        plan, approved_sha=plan.approval_sha, approval_check=lambda sha, summary: True
    )
    assert result.container_id == "new-" + tier
    create = next(call for call in fake.calls if call[:2] == ["container", "create"])
    assert create[create.index("--network") + 1] == "fixture-net"
    assert create[create.index("--entrypoint") + 1] == "entry"
    assert create[create.index("--user") + 1] == "1000"
    assert create[create.index("--workdir") + 1] == "/work"
    assert create[-2:] == [before["Image"], "serve"]
    assert "type=volume,src=" + tier + "-data,dst=/data" in create
    assert "--env-file" in create
    assert dict(result.labels)["ddak.project"] == "fixture"
    backup = next(s for s in fake.containers.values() if s["Id"] == before["Id"])
    assert backup["State"]["Running"] is False
    assert backup["Mounts"] == before["Mounts"] == fake.containers[tier]["Mounts"]
    assert not any(
        call[:2] in [["container", "rm"], ["volume", "rm"], ["volume", "create"]]
        for call in fake.calls
    )


def test_candidate_mismatch_never_stops_original(setup):
    manager, fake, _ = setup
    plan = manager.plan_ownership("was")
    fake.corrupt_new = True
    with pytest.raises(DdakToolError):
        manager.apply_ownership(
            plan, approved_sha=plan.approval_sha, approval_check=lambda sha, summary: True
        )
    assert fake.containers["was"]["Id"] == "old-was"
    assert fake.containers["was"]["State"]["Running"]
    assert not any(call[:2] == ["container", "stop"] for call in fake.calls)


def test_transfer_failed_start_preserves_backup_and_volume_no_success(setup):
    manager, fake, _ = setup
    plan = manager.plan_ownership("was")
    fake.fail = ["container", "start"]
    with pytest.raises(DdakToolError) as caught:
        manager.apply_ownership(
            plan, approved_sha=plan.approval_sha, approval_check=lambda sha, summary: True
        )
    assert "private-error" not in str(caught.value)
    assert caught.value.needs_human is True
    assert any(s["Id"] == "old-was" for s in fake.containers.values())
    assert all(
        s["Mounts"][0]["Name"] == "was-data"
        for name, s in fake.containers.items()
        if name.startswith("was")
    )


def test_foreign_db_needs_context_and_never_relabels_or_recreates(setup):
    manager, fake, _ = setup
    fake.containers["db"]["Config"]["Labels"]["ddak.project"] = "foreign"
    with pytest.raises(DdakToolError):
        manager.plan_ownership("db")
    assert not any(call[:2] == ["container", "create"] for call in fake.calls)


def test_nonbootstrap_database_rejected_without_any_command(setup):
    manager, fake, ctx = setup
    manager.ctx = replace(ctx, mode=RunMode.UPDATE)
    with pytest.raises(DdakToolError):
        manager.plan_database(database="appdb", backup_database="backupdb", accounts=ACCOUNTS)
    assert not fake.calls
