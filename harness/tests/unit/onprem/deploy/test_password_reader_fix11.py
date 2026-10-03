"""source=fake: Vault reader는 승인 후 신규 계정 생성 때만 사용한다."""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace

import pytest

from ddak.core.contracts.errors import DdakToolError
from ddak.onprem.deploy import OnPremPreparationManager
from tests.unit.onprem.deploy.test_preparation_fix11 import ACCOUNTS
from tests.unit.onprem.deploy.test_setup_adapter_fix11 import setup as setup


@pytest.fixture(autouse=True)
def no_real_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("실제 프로세스 금지")

    monkeypatch.setattr(subprocess, "Popen", forbidden)


def manager_with_reader(setup, reader):
    previous, fake, ctx = setup
    manager = OnPremPreparationManager(
        previous.root, ctx, runner=fake, stdin_runner=fake.stdin, password_reader=reader
    )
    return manager, fake


def plan_database(manager):
    return manager.plan_database(database="appdb", backup_database="backupdb", accounts=ACCOUNTS)


def apply_database(manager, plan):
    return manager.apply_database(
        plan, approved_sha=plan.approval_sha, approval_check=lambda sha, summary: True
    )


def test_injected_reader_runs_only_after_approval_and_never_reads_password_process_env(
    setup, monkeypatch, caplog
):
    calls = []
    vault = {"APP_PASSWORD": "a" * 64, "MIGRATOR_PASSWORD": "b" * 64}

    def reader(ref):
        calls.append(ref)
        return vault.get(ref)

    original_get = os.environ.get

    def guarded_get(key, *args):
        if key in vault:
            pytest.fail("주입된 reader가 있으면 비밀번호 process env를 읽으면 안 된다")
        return original_get(key, *args)

    monkeypatch.setattr(os.environ, "get", guarded_get)
    manager, fake = manager_with_reader(setup, reader)
    plan = plan_database(manager)
    assert calls == []
    with pytest.raises(DdakToolError):
        manager.apply_database(
            plan, approved_sha=plan.approval_sha, approval_check=lambda sha, summary: False
        )
    assert calls == [] and fake.stdin_calls == []
    result = apply_database(manager, plan)
    assert calls == ["APP_PASSWORD", "MIGRATOR_PASSWORD"]
    assert len(fake.stdin_calls) == 2
    for password in vault.values():
        assert password in "".join(payload for _, payload in fake.stdin_calls)
        assert password not in repr(fake.calls) + repr(result) + caplog.text
    assert manager.routed.payload is None


@pytest.mark.parametrize("missing", [None, ""])
def test_injected_reader_missing_value_does_not_fall_back_to_environment(
    setup, monkeypatch, missing
):
    monkeypatch.setenv("APP_PASSWORD", "process-value-must-not-be-used")
    manager, fake = manager_with_reader(setup, lambda ref: missing)
    with pytest.raises(DdakToolError) as caught:
        apply_database(manager, plan_database(manager))
    assert fake.stdin_calls == []
    assert "process-value" not in str(caught.value)
    assert caught.value.needs_human is True


def test_reader_exception_is_redacted_before_any_account_password_is_sent(setup, caplog):
    def failed_reader(ref):
        raise RuntimeError("private-vault-value")

    manager, fake = manager_with_reader(setup, failed_reader)
    with pytest.raises(DdakToolError) as caught:
        apply_database(manager, plan_database(manager))
    assert "private-vault-value" not in str(caught.value) + caplog.text
    assert fake.stdin_calls == [] and manager.routed.payload is None


@pytest.mark.parametrize(
    "invalid", [b"bytes-value", 123, "bad\nvalue", "bad\rvalue", "bad\0value", "x" * 4097]
)
def test_invalid_reader_values_fail_closed_without_password_stdin(setup, invalid):
    manager, fake = manager_with_reader(setup, lambda ref: invalid)
    with pytest.raises(DdakToolError):
        apply_database(manager, plan_database(manager))
    assert fake.stdin_calls == []


def test_existing_accounts_are_visible_to_main_and_never_get_generated_passwords(setup):
    def forbidden_reader(ref):
        pytest.fail("기존 계정의 비밀번호를 생성하거나 변경하면 안 된다")

    manager, fake = manager_with_reader(setup, forbidden_reader)
    fake.db.state = replace(fake.db.state, accounts=("app_user", "migrator"))
    plan = plan_database(manager)
    assert set(plan.observed.accounts) == {"app_user", "migrator"}
    assert all(action.kind != "create_account" for action in plan.actions)
    apply_database(manager, plan)
    assert fake.stdin_calls == []


def test_uninjected_reader_keeps_process_environment_compatibility(setup, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "legacy-app-fixture")
    monkeypatch.setenv("MIGRATOR_PASSWORD", "legacy-migration-fixture")
    manager, fake = manager_with_reader(setup, None)
    apply_database(manager, plan_database(manager))
    assert len(fake.stdin_calls) == 2
    assert "legacy-app-fixture" in fake.stdin_calls[0][1]
    assert "legacy-app-fixture" not in repr(fake.calls)
