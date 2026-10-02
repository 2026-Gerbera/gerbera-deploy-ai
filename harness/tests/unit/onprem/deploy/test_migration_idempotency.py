"""source=fake: 가짜 Docker 경계와 임시 SQLite DB로 중복 적용·데이터 보존을 검사한다."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from ddak.cd.tools.prepare_db.tool import prepare_db
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.tools.prepare_db import PrepareDbInput
from ddak.onprem.deploy import OnPremProvider
from tests.unit.onprem.deploy.test_onprem import FakeDocker, digest


class MigrationDocker(FakeDocker):
    def __init__(self, database):
        super().__init__()
        self.database = database
        self.phases = []
        self.up_calls = 0
        self.precheck_failure = None
        self.verify_failure = None
        self.results = {}

    def __call__(self, argv, *, timeout):
        result = super().__call__(argv, timeout=timeout)
        args = argv[3:]
        if args[:2] == ["container", "start"]:
            phase = self.find(args[-1])["phase"]
            self.phases.append(phase)
            applied = []
            if phase == "up":
                self.up_calls += 1
                # 반복하면 CREATE가 실패하는 러너로 provider의 생략 여부를 증명한다.
                self.database.executescript(
                    "CREATE TABLE schema_version (version TEXT PRIMARY KEY);"
                    "INSERT INTO schema_version VALUES ('0001');"
                    "CREATE TABLE payload (id INTEGER PRIMARY KEY, value TEXT NOT NULL);"
                )
                applied = ["0001"]
            has_version = self.database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            current = (
                self.database.execute("SELECT version FROM schema_version").fetchone()[0]
                if has_version
                else None
            )
            self.results[phase] = {
                "phase": phase,
                "ok": phase == "precheck" or current == "0001",
                "current": current,
                "expected": "0001",
                "applied": applied,
                "signature": digest("fixture-schema"),
                "fingerprint": {
                    "version": sqlite3.sqlite_version,
                    "sql_mode": "",
                    "collation": "BINARY",
                    "time_zone": "UTC",
                    "ssl_version": "",
                },
            }
        elif args[:2] == ["container", "wait"]:
            if self.find(args[-1])["phase"] == "precheck" and self.precheck_failure == "exit":
                result.stdout = "1\n"
        elif args[:2] == ["container", "logs"]:
            phase = self.find(args[-1])["phase"]
            data = dict(self.results[phase])
            if phase == "precheck":
                if self.precheck_failure == "ok":
                    data["ok"] = False
                elif self.precheck_failure == "phase":
                    data["phase"] = "verify"
                elif self.precheck_failure == "json":
                    result.stdout = "not-json"
                    return result
            elif phase == "verify":
                if self.verify_failure == "ok":
                    data["ok"] = False
                elif self.verify_failure == "current":
                    data["current"] = "0000"
            result.stdout = "MIGRATE_RESULT " + json.dumps(data)
        return result


@pytest.fixture
def migration_runtime(tmp_path, monkeypatch):
    import ddak.cd.dispatch as dispatch

    with closing(sqlite3.connect(tmp_path / "fixture.db")) as database:
        fake = MigrationDocker(database)
        provider = OnPremProvider(fake)
        monkeypatch.setattr(dispatch, "OnPremProvider", lambda: provider)
        ctx = RunContext(
            "migration-run",
            project="migration-project",
            adapter_mode=AdapterMode.REAL,
            lock_token="fixture-lock",
            images={"was": fake.refs[0]},
            platform={
                "onprem": {
                    "docker_host": "unix:///test.sock",
                    "tiers": {"was": {"name": "fixture-was", "platform": "linux/arm64"}},
                }
            },
        )
        yield fake, provider, ctx


def prepare(ctx, migrations=()):
    return prepare_db(
        PrepareDbInput(
            run_id=ctx.run_id,
            lock_token=ctx.lock_token,
            target="local",
            migrations=list(migrations),
        ),
        ctx,
    )


def seed_data(fake, ctx):
    assert prepare(ctx).changed
    fake.database.execute("INSERT INTO payload VALUES (1, 'preserved')")
    fake.database.commit()
    fake.phases.clear()


def assert_preserved(fake, statements):
    assert fake.database.execute("SELECT * FROM payload").fetchall() == [(1, "preserved")]
    assert not any(
        sql.lstrip().upper().startswith(("DROP", "CREATE", "ALTER", "DELETE", "INSERT", "UPDATE"))
        for sql in statements
    )
    assert not fake.containers


def test_dbinit_then_explicit_migration_applies_only_once(migration_runtime):
    fake, _, ctx = migration_runtime
    first = prepare(ctx)
    fake.database.execute("INSERT INTO payload VALUES (1, 'preserved')")
    fake.database.commit()
    statements = []
    fake.database.set_trace_callback(statements.append)
    second = prepare(ctx, ["0001"])
    assert first.changed
    assert not second.changed
    assert fake.up_calls == 1
    assert fake.phases == ["precheck", "up", "verify", "precheck", "verify"]
    assert [p["phase"] for p in second.migration["phases"]] == ["precheck", "verify"]
    assert second.migration["current"] == second.migration["expected"] == "0001"
    assert second.migration["applied"] == []
    assert_preserved(fake, statements)


def test_new_controller_dbinit_preserves_existing_data(migration_runtime, monkeypatch):
    import ddak.cd.dispatch as dispatch

    fake, provider, ctx = migration_runtime
    seed_data(fake, ctx)
    statements = []
    fake.database.set_trace_callback(statements.append)
    new_provider = OnPremProvider(fake)
    assert new_provider is not provider
    monkeypatch.setattr(dispatch, "OnPremProvider", lambda: new_provider)
    result = prepare(replace(ctx, run_id="new-controller-run"))
    assert not result.changed and fake.up_calls == 1
    assert fake.phases == ["precheck", "verify"]
    assert_preserved(fake, statements)


@pytest.mark.parametrize("failure", ["ok", "exit", "json", "phase"])
def test_failed_precheck_never_runs_up_or_recreates_data(migration_runtime, failure):
    fake, _, ctx = migration_runtime
    seed_data(fake, ctx)
    statements = []
    fake.database.set_trace_callback(statements.append)
    fake.precheck_failure = failure
    with pytest.raises(DdakToolError):
        prepare(ctx)
    assert fake.phases == ["precheck"] and fake.up_calls == 1
    assert_preserved(fake, statements)


@pytest.mark.parametrize("failure", ["ok", "current", "requested"])
def test_already_applied_still_requires_verify(migration_runtime, failure):
    fake, _, ctx = migration_runtime
    seed_data(fake, ctx)
    statements = []
    fake.database.set_trace_callback(statements.append)
    fake.verify_failure = failure
    with pytest.raises(DdakToolError):
        prepare(ctx, ["0002"] if failure == "requested" else ["0001"])
    assert fake.phases == (["precheck"] if failure == "requested" else ["precheck", "verify"])
    assert fake.up_calls == 1
    assert_preserved(fake, statements)


def test_requested_version_mismatch_stops_before_first_up(migration_runtime):
    fake, _, ctx = migration_runtime
    with pytest.raises(DdakToolError, match="up 실행 차단"):
        prepare(ctx, ["0002"])
    assert fake.phases == ["precheck"] and fake.up_calls == 0
    assert not fake.database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
