"""source=fake: 등록·DB 준비·소유권 이전은 실제 VM/프로세스 없이 검증한다."""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import subprocess
from dataclasses import replace

import pytest

from ddak.core.contracts.errors import DdakToolError
from ddak.onprem.deploy import (
    ContainerObservation,
    DatabaseAccount,
    DatabaseAction,
    DatabaseObservation,
    InventoryConfig,
    TableObservation,
    apply_db_preparation,
    apply_owner_transfer,
    plan_db_preparation,
    plan_owner_transfer,
    read_inventory,
    register_onprem_inventory,
    write_inventory,
)


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("외부 프로세스 금지")

    monkeypatch.setattr(subprocess, "Popen", forbidden)


def inventory():
    return {
        "mode": "vm",
        "public_url": "https://app.example.test",
        "ssh": {
            "host": "192.0.2.2",
            "user": "operator",
            "key_path": "/missing/key",
            "host_key_fingerprint": "SHA256:" + "A" * 43,
        },
        "tiers": {
            "web": {
                "name": "web",
                "kind": "nginx",
                "platform": "linux/arm64",
                "network": "web-net",
                "public_env": {"WAS_UPSTREAM": "was:8000"},
            },
            "was": {"name": "was", "platform": "linux/arm64", "network": "was-net"},
            "db": {
                "name": "db",
                "kind": "mysql",
                "platform": "linux/arm64",
                "network": "db-net",
                "volumes": [{"name": "db-data", "target": "/var/lib/mysql"}],
            },
        },
    }


def test_product_registration_paths_permissions_public_model_and_no_values_logged(tmp_path, caplog):
    data = inventory()
    original = copy.deepcopy(data)
    path = write_inventory(tmp_path, "fixture", data)
    assert path == tmp_path / "settings/fixture/inventory.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    result = read_inventory(path)
    assert isinstance(result, InventoryConfig)
    assert result.tiers["was"].env_file == str(tmp_path / "private/fixture/runtime.env")
    assert result.tiers["was"].migration_env_file == str(tmp_path / "private/fixture/migration.env")
    assert result.tiers["web"].ready.path == "/nginx-health"
    assert result.tiers["web"].ready.port == 8080
    assert result.tiers["web"].public_env == {"WAS_UPSTREAM": "was:8000"}
    assert data == original
    assert not (tmp_path / "private").exists()
    assert not caplog.records


def test_registration_preserves_accepted_inventory_and_explicit_paths(tmp_path):
    data = inventory()
    data["tiers"]["web"]["ready"] = {"port": 81, "path": "/custom"}
    data["tiers"]["was"].update(env_file="/missing/runtime", migration_env_file="/missing/migrate")
    path = write_inventory(tmp_path, "fixture", data)
    assert read_inventory(path).model_dump() == InventoryConfig.model_validate(data).model_dump()
    # 기존 container 모드도 그대로 허용한다.
    container = {"tiers": {"was": {"name": "app", "platform": "linux/amd64"}}}
    path = register_onprem_inventory(json.dumps(container), tmp_path / "container.json")
    assert read_inventory(path).mode == "container"


@pytest.mark.parametrize("bad", ["../escape", "foo/bar", "..", "", "a\n"])
def test_registration_rejects_project_traversal_before_writing(tmp_path, bad):
    with pytest.raises(DdakToolError):
        write_inventory(tmp_path, bad, inventory())
    assert list(tmp_path.iterdir()) == []


def test_invalid_values_are_redacted_and_old_inventory_survives(tmp_path, caplog):
    path = write_inventory(tmp_path, "fixture", inventory())
    before = path.read_bytes()
    data = inventory()
    sentinel = "sensitive-value-never-report"
    data["tiers"]["was"]["public_env"] = {"PASSWORD": sentinel}
    with pytest.raises(DdakToolError) as caught:
        write_inventory(tmp_path, "fixture", data)
    assert sentinel not in str(caught.value)
    assert sentinel not in caplog.text
    assert path.read_bytes() == before


@pytest.mark.parametrize("raw", ['{"tiers":{},"tiers":{}}', "[]", "NaN", "x" * 65537])
def test_bad_json_does_not_write(tmp_path, raw):
    with pytest.raises(DdakToolError):
        register_onprem_inventory(raw, tmp_path / "inventory.json")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["file-symlink", "parent-symlink", "hardlink", "unsafe-parent"])
def test_registration_rejects_unsafe_paths(tmp_path, kind):
    safe = tmp_path / "safe"
    safe.mkdir()
    destination = safe / "inventory.json"
    other = tmp_path / "other"
    other.write_text("preserve")
    if kind == "file-symlink":
        destination.symlink_to(other)
    elif kind == "parent-symlink":
        (tmp_path / "linked").symlink_to(safe, target_is_directory=True)
        destination = tmp_path / "linked/inventory.json"
    elif kind == "hardlink":
        os.link(other, destination)
    else:
        safe.chmod(0o777)
    with pytest.raises(DdakToolError):
        register_onprem_inventory(json.dumps(inventory()), destination)
    assert other.read_text() == "preserve"


def test_atomic_failure_preserves_old_file_and_removes_temporary(tmp_path, monkeypatch):
    path = write_inventory(tmp_path, "fixture", inventory())
    before = path.read_bytes()

    def failed(*args, **kwargs):
        raise OSError("sensitive-I/O-details")

    monkeypatch.setattr(os, "replace", failed)
    with pytest.raises(DdakToolError) as caught:
        write_inventory(tmp_path, "fixture", inventory())
    assert "sensitive" not in str(caught.value)
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("bad_mode", [0o644, 0o400])
def test_read_checks_permissions(tmp_path, bad_mode):
    path = write_inventory(tmp_path, "fixture", inventory())
    path.chmod(bad_mode)
    with pytest.raises(DdakToolError):
        read_inventory(path)


ACCOUNTS = (
    DatabaseAccount("app_user", "app", "APP_PASSWORD"),
    DatabaseAccount("migrator", "migrator", "MIGRATOR_PASSWORD"),
)


class FakeDatabase:
    def __init__(self, *, existing=True):
        self.state = DatabaseObservation(
            "vm-fixture/mysql",
            ("appdb",) if existing else (),
            (TableObservation("appdb", "posts"), TableObservation("appdb", "other_app"))
            if existing
            else (),
            (),
        )
        self.calls = []
        self.ignore = None

    def observe(self, database, backup_database, accounts):
        assert database == "appdb" and backup_database == "backupdb"
        assert accounts == ("app_user", "migrator")
        return self.state

    def execute(self, sql):
        self.calls.append(sql)
        if sql.startswith(self.ignore or "no-match"):
            return
        if sql.startswith("CREATE DATABASE"):
            name = re.fullmatch(r"CREATE DATABASE `([A-Za-z_]+)`", sql)[1]
            self.state = replace(self.state, databases=tuple(sorted((*self.state.databases, name))))
        elif sql.startswith("RENAME TABLE"):
            pairs = re.findall(r"`appdb`\.`([A-Za-z_]+)` TO `backupdb`\.`([A-Za-z_]+)`", sql)
            assert len(pairs) == len(self.state.tables)
            assert all(source == target for source, target in pairs)
            self.state = replace(
                self.state,
                tables=tuple(replace(table, database="backupdb") for table in self.state.tables),
            )
        elif sql.startswith("GRANT"):
            match = re.fullmatch(r"GRANT ([A-Z, ]+) ON `appdb`\.\* TO '([A-Za-z_]+)'@'%'", sql)
            privileges = tuple(sorted(match[1].split(", ")))
            grants = {(name, db): values for name, db, values in self.state.grants}
            grants[(match[2], "appdb")] = privileges
            self.state = replace(
                self.state,
                grants=tuple(sorted((name, db, values) for (name, db), values in grants.items())),
            )
        else:
            pytest.fail("허용되지 않은 SQL")

    def create_account(self, name, *, password_env_ref):
        self.calls.append(("create_account", name, password_env_ref))
        assert password_env_ref in {"APP_PASSWORD", "MIGRATOR_PASSWORD"}
        self.state = replace(self.state, accounts=tuple(sorted((*self.state.accounts, name))))


def db_plan(fake):
    return plan_db_preparation(
        project="fixture",
        database="appdb",
        backup_database="backupdb",
        accounts=ACCOUNTS,
        session=fake,
    )


def db_apply(plan, fake, **kwargs):
    return apply_db_preparation(
        plan,
        session=fake,
        approved_sha=plan.approval_sha,
        approval_check=lambda sha, summary: True,
        **kwargs,
    )


def test_other_app_tables_bound_to_approval_and_moved_once_without_drop(caplog):
    fake = FakeDatabase()
    plan = db_plan(fake)
    assert not fake.calls
    summary = json.loads(plan.summary)
    assert summary["observed_tables"] == ["other_app", "posts"]
    approved = []
    result = apply_db_preparation(
        plan,
        approved_sha=plan.approval_sha,
        session=fake,
        approval_check=lambda sha, summary: approved.append((sha, summary)) or True,
    )
    assert approved == [(plan.approval_sha, plan.summary)]
    assert result.applied == plan.actions
    assert fake.calls[0] == "CREATE DATABASE `backupdb`"
    rename = [call for call in fake.calls if isinstance(call, str) and call.startswith("RENAME")]
    assert rename == [
        "RENAME TABLE `appdb`.`other_app` TO `backupdb`.`other_app`, "
        "`appdb`.`posts` TO `backupdb`.`posts`"
    ]
    assert {(t.database, t.name) for t in fake.state.tables} == {
        ("backupdb", "posts"),
        ("backupdb", "other_app"),
    }
    assert "DROP" not in repr(fake.calls) and "PASSWORD=" not in repr(fake.calls)
    assert not caplog.records


def test_missing_db_and_accounts_created_with_env_references_only(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "secret-value-do-not-touch")
    fake = FakeDatabase(existing=False)
    result = db_apply(db_plan(fake), fake)
    assert fake.calls[0] == "CREATE DATABASE `appdb`"
    assert ("create_account", "app_user", "APP_PASSWORD") in fake.calls
    assert "secret-value" not in repr(fake.calls) + repr(result)
    assert "RENAME" not in repr(fake.calls)


@pytest.mark.parametrize(
    "bad", ["sha", "denied", "changed-tables", "changed-target", "changed-account"]
)
def test_db_approval_and_observed_changes_fail_before_mutation(bad):
    fake = FakeDatabase()
    plan = db_plan(fake)
    if bad == "changed-tables":
        fake.state = replace(
            fake.state, tables=(*fake.state.tables, TableObservation("appdb", "new"))
        )
    elif bad == "changed-target":
        fake.state = replace(fake.state, target_id="another-vm")
    elif bad == "changed-account":
        fake.state = replace(fake.state, accounts=("app_user",))
    with pytest.raises(DdakToolError):
        apply_db_preparation(
            plan,
            session=fake,
            approved_sha="0" * 64 if bad == "sha" else plan.approval_sha,
            approval_check=lambda sha, summary: bad != "denied",
        )
    assert not fake.calls


def test_arbitrary_sql_action_cannot_be_smuggled_into_approved_plan():
    fake = FakeDatabase()
    plan = db_plan(fake)
    tampered = replace(
        plan, actions=(DatabaseAction("create_database", "x`; DROP DATABASE appdb"),)
    )
    with pytest.raises(DdakToolError):
        db_apply(tampered, fake)
    assert not fake.calls


@pytest.mark.parametrize("bad", ["backup-collision", "view-or-trigger", "update"])
def test_unsafe_bootstrap_conditions_fail_closed(bad):
    fake = FakeDatabase()
    if bad == "backup-collision":
        fake.state = replace(fake.state, databases=("appdb", "backupdb"))
    elif bad == "view-or-trigger":
        fake.state = replace(fake.state, tables=(TableObservation("appdb", "posts", False),))
    with pytest.raises(DdakToolError):
        plan_db_preparation(
            project="fixture",
            database="appdb",
            backup_database="backupdb",
            accounts=ACCOUNTS,
            session=fake,
            mode="UPDATE" if bad == "update" else "BOOTSTRAP",
        )
    assert not fake.calls


@pytest.mark.parametrize("ignored", ["CREATE DATABASE", "RENAME TABLE", "GRANT"])
def test_db_success_metadata_without_observed_effect_is_rejected(ignored):
    fake = FakeDatabase()
    fake.ignore = ignored
    with pytest.raises(DdakToolError):
        db_apply(db_plan(fake), fake)
    assert "DROP" not in repr(fake.calls)
    assert len([c for c in fake.calls if isinstance(c, str) and c.startswith(ignored)]) == 1


@pytest.mark.parametrize("bad_name", ["appdb;DROP", "mysql", "app-db", "a`b"])
def test_db_identifiers_validated_before_observer(bad_name):
    fake = FakeDatabase()
    with pytest.raises(DdakToolError):
        plan_db_preparation(
            project="fixture",
            database=bad_name,
            backup_database="backupdb",
            accounts=ACCOUNTS,
            session=fake,
        )
    assert not fake.calls


def container():
    return ContainerObservation(
        "vm-fixture/docker",
        "old-id",
        "app-was",
        "image-fixed",
        "a" * 64,
        (("ddak.managed", "true"), ("ddak.project", "previous"), ("ddak.tier", "was")),
        (("data-volume", "/data", True),),
        True,
    )


def test_owner_transfer_requires_real_new_container_labels_and_preserves_volume():
    state = container()
    plan = plan_owner_transfer(project="fixture", tier="was", observe=lambda: state)
    calls = []

    def transfer(proposal):
        nonlocal state
        calls.append(proposal.approval_sha)
        state = replace(
            state,
            container_id="new-id",
            labels=tuple(sorted({**dict(state.labels), **proposal.owner_labels}.items())),
        )

    result = apply_owner_transfer(
        plan,
        approved_sha=plan.approval_sha,
        approval_check=lambda sha, summary: True,
        observe=lambda: state,
        transfer=transfer,
    )
    assert calls == [plan.approval_sha]
    assert result.container_id == "new-id" and result.volumes == plan.observed.volumes
    assert dict(result.labels)["ddak.project"] == "fixture"
    assert json.loads(plan.summary)["requires_container_replacement"] is True


@pytest.mark.parametrize(
    "bad",
    [
        "state-only",
        "immutable-labels",
        "lost-volume",
        "wrong-image",
        "changed-config",
        "wrong-host",
        "changed-before",
        "deny",
        "sha",
    ],
)
def test_owner_failures_never_claim_success(bad):
    state = container()
    plan = plan_owner_transfer(project="fixture", tier="was", observe=lambda: state)
    calls = []
    if bad == "changed-before":
        state = replace(state, running=False)

    def transfer(proposal):
        nonlocal state
        calls.append(proposal)
        if bad == "state-only":
            return
        state = replace(
            state,
            labels=tuple(sorted(proposal.owner_labels.items())),
            container_id=state.container_id if bad == "immutable-labels" else "new-id",
        )
        if bad == "lost-volume":
            state = replace(state, volumes=())
        elif bad == "wrong-image":
            state = replace(state, image_id="other-image")
        elif bad == "changed-config":
            state = replace(state, config_sha="b" * 64)
        elif bad == "wrong-host":
            state = replace(state, target_id="another-host")

    with pytest.raises(DdakToolError):
        apply_owner_transfer(
            plan,
            approved_sha="0" * 64 if bad == "sha" else plan.approval_sha,
            approval_check=lambda sha, summary: bad != "deny",
            observe=lambda: state,
            transfer=transfer,
        )
    if bad in {"changed-before", "deny", "sha"}:
        assert not calls


def test_foreign_db_labels_never_propose_recreation():
    with pytest.raises(DdakToolError):
        plan_owner_transfer(project="fixture", tier="db", observe=container)


def test_already_owned_database_is_observed_noop():
    state = replace(
        container(),
        labels=(("ddak.managed", "true"), ("ddak.project", "fixture"), ("ddak.tier", "db")),
    )
    plan = plan_owner_transfer(project="fixture", tier="db", observe=lambda: state)
    assert not plan.requires_replacement
    result = apply_owner_transfer(
        plan,
        approved_sha=plan.approval_sha,
        approval_check=lambda sha, summary: True,
        observe=lambda: state,
        transfer=lambda plan: pytest.fail("교체 금지"),
    )
    assert result == plan.observed
