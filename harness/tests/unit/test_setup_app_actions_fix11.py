"""source=fixture: main 조립 -> 승인 -> 가짜 DB -> 분리된 private env. 실제 VM 호출 없음."""

from dataclasses import replace
from urllib.parse import urlsplit

import pytest

from ddak import app
from ddak.core.config import AdapterMode, Settings
from ddak.core.contracts.errors import DdakToolError
from ddak.core.registry import Registry
from ddak.executor.service import DeploymentService
from ddak.onprem.deploy import apply_db_preparation, plan_db_preparation
from tests.unit.onprem.deploy.test_preparation_fix11 import FakeDatabase, inventory
from tests.unit.test_setup_service_fix11 import Builder


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("DDAK_ONPREM_INVENTORY", raising=False)
    service = DeploymentService(Registry([]), tmp_path.resolve() / "state")
    # DB 준비 fixture는 최초 실행의 CLI 기본값과 실행 호스트에 의존하지 않는다.
    settings = Settings(
        adapter_mode=AdapterMode.REAL, llm_provider="replay", judgment_provider="replay"
    )
    setup = app._setup_service(service, settings, "127.0.0.1")
    setup._build_factory = Builder
    service.onboarding = setup
    setup.register_inventory("flaskr", inventory())
    db = FakeDatabase()
    passwords = []

    class Manager:
        def __init__(self, root, ctx, *, password_reader):
            self.ctx = ctx
            self.password_reader = password_reader

        def plan_database(self, **kwargs):
            return plan_db_preparation(project=self.ctx.project, session=db, **kwargs)

        def apply_database(self, plan, **kwargs):
            def create(name, *, password_env_ref):
                value = self.password_reader(password_env_ref)
                assert value and len(value) == 64
                passwords.append(value)
                db.calls.append(("create_account", name))
                db.state = replace(db.state, accounts=tuple(sorted((*db.state.accounts, name))))

            db.create_account = create
            return apply_db_preparation(plan, session=db, **kwargs)

    monkeypatch.setattr(app, "OnPremPreparationManager", Manager)
    actions = app._setup_actions(service, settings)
    yield service, setup, actions, db, passwords
    service.close()


ARGS = {
    "database": "appdb",
    "backup_database": "backupdb",
    "app_account": "app_user",
    "migrator_account": "migrator",
}


def test_approval_creates_accounts_preserves_tables_and_separates_urls(rig):
    service, setup, actions, db, passwords = rig
    proposal = actions.plan("flaskr", "database", ARGS)
    assert "other_app" in proposal["summary"]
    assert not db.calls and not passwords
    result = actions.apply("flaskr", proposal["id"], proposal["hash"], "operator")
    assert result["status"] == "SUCCEEDED"
    assert not service.store.environments("flaskr")  # 준비는 배포 성공 기록이 아니다.
    assert service.project_state("flaskr")["lock"] is None
    assert {table.database for table in db.state.tables} == {"backupdb"}
    runtime = setup.runtime_env("flaskr").read_text()
    migration = setup.runtime_env("flaskr").with_name("migration.env").read_text()
    assert "MIGRATOR" not in runtime
    assert urlsplit(runtime.strip().split("=", 1)[1]).username == "app_user"
    assert "DATABASE_URL_MIGRATOR=" in migration
    assert all(
        urlsplit(line.split("=", 1)[1]).username == "migrator" for line in migration.splitlines()
    )
    public = str(actions.view("flaskr")) + str(setup.view("flaskr"))
    assert all(value not in public for value in passwords)
    assert setup.runtime_env("flaskr").with_name("migration.env").stat().st_mode & 0o777 == 0o600


def test_existing_account_without_saved_password_requires_new_name(rig):
    _, _, actions, db, _ = rig
    db.state = replace(db.state, accounts=("app_user",))
    with pytest.raises(DdakToolError, match="기존 계정 암호"):
        actions.plan("flaskr", "database", ARGS)
    assert not db.calls


def test_inventory_change_after_approval_blocks_db_before_execution(rig):
    _, setup, actions, db, _ = rig
    proposal = actions.plan("flaskr", "database", ARGS)
    changed = inventory()
    changed["ssh"]["host"] = "192.0.2.3"
    setup.register_inventory("flaskr", changed)
    with pytest.raises(DdakToolError, match="인벤토리 변경"):
        actions.apply("flaskr", proposal["id"], proposal["hash"], "operator")
    assert not db.calls


def test_post_db_secret_save_failure_blocks_environment(rig):
    service, setup, actions, db, _ = rig
    proposal = actions.plan("flaskr", "database", ARGS)
    path = setup.runtime_env("flaskr").with_name("migration.env")
    path.write_text("")
    path.chmod(0o644)
    with pytest.raises(DdakToolError) as caught:
        actions.apply("flaskr", proposal["id"], proposal["hash"], "operator")
    assert caught.value.needs_human
    assert service.store.run(proposal["id"])["status"] == "NEEDS_HUMAN"
    assert service.project_state("flaskr")["blocked_targets"] == ["local"]
    assert {table.database for table in db.state.tables} == {"backupdb"}


def test_registered_migration_file_is_the_only_write_target(rig, tmp_path):
    _, setup, actions, _, _ = rig
    data = inventory()
    custom = tmp_path.resolve() / "private-custom" / "migrate.env"
    data["tiers"]["was"]["migration_env_file"] = str(custom)
    setup.register_inventory("flaskr", data)
    proposal = actions.plan("flaskr", "database", ARGS)
    result = actions.apply("flaskr", proposal["id"], proposal["hash"], "operator")
    assert result["status"] == "SUCCEEDED"
    assert custom.exists() and custom.stat().st_mode & 0o777 == 0o600
    assert not setup.runtime_env("flaskr").with_name("migration.env").exists()
