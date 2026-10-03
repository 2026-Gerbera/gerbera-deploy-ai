"""격리된 가짜 값만 사용하는 제품 vault 검증."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ddak.core.contracts.errors import DdakToolError
from ddak.core.private_values import SecretVault


def test_vault_roundtrip_isolated_and_private(tmp_path: Path, capsys, caplog) -> None:
    vault = SecretVault(tmp_path / "private" / "vault")
    value = "fake-" + "private-value"
    assert vault.get("project", "anthropic_api_key") is None
    assert not (tmp_path / "private").exists()
    vault.put("project", "anthropic_api_key", value)
    vault.put("other", "anthropic_api_key", "other-" + "fake")
    assert vault.get("project", "anthropic_api_key") == value
    assert vault.get("other", "anthropic_api_key") != value
    assert vault.configured("project", "anthropic_api_key") is True
    assert vault.path.stat().st_mode & 0o777 == 0o700
    assert vault.path.parent.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in vault.path.iterdir())
    assert all("anthropic" not in p.name and "project" not in p.name for p in vault.path.iterdir())
    assert value not in repr(vault)
    assert not capsys.readouterr().out and not caplog.text
    assert vault.delete("project", "anthropic_api_key") is True
    assert vault.delete("project", "anthropic_api_key") is False
    assert vault.get("other", "anthropic_api_key") is not None


@pytest.mark.parametrize("part", ["..", ".", "/absolute", "with/slash", "bad\\x00key", ""])
def test_invalid_identifiers_are_generic(tmp_path: Path, part: str) -> None:
    vault = SecretVault(tmp_path / "vault")
    with pytest.raises(DdakToolError) as error:
        vault.put(part, "key", "fake-" + "secret")
    assert part not in str(error.value) if part else True
    assert "fake-secret" not in str(error.value)
    assert not vault.path.exists()


def test_symlink_ancestors_leaf_and_value_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DdakToolError):
        SecretVault(tmp_path / "link" / "vault")
    with pytest.raises(DdakToolError):
        SecretVault(str(tmp_path) + "/../vault")
    vault = SecretVault(tmp_path / "vault")
    vault.put("project", "key", "fake-value")
    path = next(vault.path.iterdir())
    path.unlink()
    external = outside / "untouched"
    external.write_text("not-" + "a-secret")
    external.chmod(0o600)
    path.symlink_to(external)
    for operation in (
        lambda: vault.get("project", "key"),
        lambda: vault.put("project", "key", "replacement"),
        lambda: vault.delete("project", "key"),
        lambda: vault.configured("project", "key"),
    ):
        with pytest.raises(DdakToolError):
            operation()
    assert external.read_text() == "not-a-secret"


@pytest.mark.parametrize("kind", ["permissions", "hardlink", "fifo", "folder"])
def test_invalid_value_files_are_rejected(tmp_path: Path, kind: str) -> None:
    vault = SecretVault(tmp_path / "vault")
    vault.put("project", "key", "fake-value")
    path = next(vault.path.iterdir())
    if kind == "permissions":
        path.chmod(0o644)
    elif kind == "hardlink":
        os.link(path, tmp_path / "link")
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path, mode=0o600)
    else:
        path.unlink()
        path.mkdir(mode=0o700)
    with pytest.raises(DdakToolError):
        vault.get("project", "key")


def test_parent_replaced_with_link_after_constructor(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "root" / "vault")
    (tmp_path / "root").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DdakToolError):
        vault.put("project", "key", "fake-value")
    assert not (tmp_path / "vault").exists()


def test_directory_permissions_not_silently_changed(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir(mode=0o755)
    vault = SecretVault(root)
    with pytest.raises(DdakToolError):
        vault.put("project", "key", "fake-value")
    assert root.stat().st_mode & 0o777 == 0o755


def test_concurrent_writes_preserve_all_keys(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "vault")
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda n: vault.put("project", f"key_{n}", f"fake-{n}"), range(20)))
    assert all(vault.get("project", f"key_{n}") == f"fake-{n}" for n in range(20))
    assert not list(vault.path.glob(".pending-*"))


def test_export_env_atomic_private_and_missing_or_multiline_denied(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "vault")
    vault.put("project", "runtime_API_KEY", "fake-" + "api-value")
    vault.put("project", "runtime_DB_HOST", "localhost")
    target = tmp_path / "runtime" / "was.env"
    vault.export_env(
        "project", target, {"API_KEY": "runtime_API_KEY", "DB_HOST": "runtime_DB_HOST"}
    )
    assert target.read_text() == "API_KEY=fake-api-value\nDB_HOST=localhost\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    before = target.read_bytes()
    with pytest.raises(DdakToolError):
        vault.export_env("project", target, {"DB_HOST": "missing"})
    vault.put("project", "runtime_DB_HOST", "host\nINJECTED=yes")
    with pytest.raises(DdakToolError) as error:
        vault.export_env("project", target, {"DB_HOST": "runtime_DB_HOST"})
    assert target.read_bytes() == before
    assert "INJECTED" not in str(error.value)
    assert not list(target.parent.glob(".pending-*"))


def test_export_env_rejects_existing_symlink(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "vault")
    vault.put("project", "runtime_KEY", "fake-value")
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    (root / "env").symlink_to(outside)
    with pytest.raises(DdakToolError):
        vault.export_env("project", root / "env", {"KEY": "runtime_KEY"})
    assert outside.read_text() == "untouched"


@pytest.mark.parametrize("value", ["", "bad\x00value", "x" * 65537, "\ud800"])
def test_invalid_values_are_not_exposed(tmp_path: Path, value: str) -> None:
    vault = SecretVault(tmp_path / "vault")
    with pytest.raises(DdakToolError) as error:
        vault.put("project", "key", value)
    assert "bad" not in str(error.value)
    assert not vault.path.exists()


def test_atomic_write_failure_cleans_pending_and_preserves_value(tmp_path: Path, monkeypatch):
    from ddak.core import private_values

    vault = SecretVault(tmp_path / "vault")
    vault.put("project", "key", "old-value")

    def failed_replace(*args, **kwargs):
        raise OSError("fake-" + "private-output")

    monkeypatch.setattr(private_values.os, "replace", failed_replace)
    with pytest.raises(DdakToolError) as error:
        vault.put("project", "key", "new-value")
    assert "fake-private-output" not in str(error.value)
    assert vault.get("project", "key") == "old-value"
    assert not list(vault.path.glob(".pending-*"))


@pytest.mark.parametrize("name", ["../outside", "/outside", ".", "..", "bad\\name", "bad\x00name"])
def test_secure_file_helpers_reject_nonbasename_names(tmp_path: Path, name: str) -> None:
    from ddak.core.private_values import private_directory, read_private, write_private

    with private_directory(tmp_path / "private", create=True) as fd:
        with pytest.raises(DdakToolError):
            read_private(fd, name)
        with pytest.raises(DdakToolError):
            write_private(fd, name, b"fake-value")


def test_empty_corrupt_vault_file_is_not_configured(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "vault")
    vault.put("project", "key", "fake-value")
    next(vault.path.iterdir()).write_bytes(b"")
    with pytest.raises(DdakToolError):
        vault.configured("project", "key")
