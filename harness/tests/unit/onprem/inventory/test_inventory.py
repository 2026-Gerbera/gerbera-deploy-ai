"""onprem/inventory: platform.onprem.yaml 읽기·검증."""

from __future__ import annotations

from pathlib import Path

import pytest

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.onprem.deploy import InventoryConfig, TierConfig
from ddak.onprem.inventory import load_inventory

GOOD = """
docker_host: unix:///var/run/docker.sock
tiers:
  was:
    name: demo-was
    platform: linux/arm64
    ports: ["127.0.0.1:8080:8000"]
    network: bridge
    volumes: [{name: demo-data, target: /data, read_only: false}]
    env_file: /private/runtime/demo/was.env
    public_env: {APP_BASE_URL: "http://localhost:8080", DB_HOST: db}
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "platform.onprem.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _bad(tmp_path: Path, text: str) -> DdakToolError:
    with pytest.raises(DdakToolError) as info:
        load_inventory(_write(tmp_path, text))
    assert info.value.code == ErrorCode.CONFIG_INVALID
    return info.value


def test_good_inventory_is_accepted_by_provider_model(tmp_path: Path) -> None:
    result = load_inventory(_write(tmp_path, GOOD))
    assert result["docker_host"] == "unix:///var/run/docker.sock"
    InventoryConfig.model_validate(result)  # O1 입력 모델이 받아들인다


def test_minimal_inventory_without_docker_host(tmp_path: Path) -> None:
    text = "tiers:\n  was: {name: demo-was, platform: linux/amd64}\n"
    result = load_inventory(_write(tmp_path, text))
    assert result["docker_host"] is None
    InventoryConfig.model_validate(result)


def test_tier_fields_match_provider() -> None:
    assert InventoryConfig.model_fields["tiers"].annotation == dict[str, TierConfig]


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DdakToolError) as info:
        load_inventory(tmp_path / "nope.yaml")
    assert info.value.code == ErrorCode.CONFIG_INVALID


def test_top_level_not_mapping(tmp_path: Path) -> None:
    _bad(tmp_path, "- a\n- b\n")


def test_bad_tier_name(tmp_path: Path) -> None:
    _bad(tmp_path, "tiers:\n  BAD_Name: {name: x, platform: linux/amd64}\n")


def test_secret_like_key_rejected_without_leaking_value(tmp_path: Path) -> None:
    fake = "hunter" + "2"
    err = _bad(tmp_path, GOOD.replace("DB_HOST: db", f"db_password: {fake}"))
    assert fake not in str(err)


def test_non_local_docker_host(tmp_path: Path) -> None:
    _bad(tmp_path, GOOD.replace("unix:///var/run/docker.sock", "ssh://root@host"))


def test_unknown_field(tmp_path: Path) -> None:
    _bad(tmp_path, GOOD.replace("network: bridge", "network: bridge\n    privileged: true"))


def test_mysql_requires_protected_volume(tmp_path: Path) -> None:
    _bad(tmp_path, "tiers:\n  db: {name: demo-db, platform: linux/amd64}\n")
    _bad(tmp_path, "tiers:\n  db: {name: demo-db, kind: mysql, platform: linux/amd64}\n")
    text = """
tiers:
  db:
    name: demo-db
    platform: linux/amd64
    kind: mysql
    volumes: [{name: demo-mysql, target: /var/lib/mysql}]
"""
    InventoryConfig.model_validate(load_inventory(_write(tmp_path, text)))


def test_vm_tier_ssh_and_migration_fields(tmp_path: Path) -> None:
    text = """
mode: vm
public_url: https://demo.example.com
tiers:
  was:
    name: demo-was
    platform: linux/amd64
    network: demo-net
    migration_env_file: /private/migrate.env
    ready: {port: 8000, path: /health/ready, timeout_s: 30}
    ssh:
      host: 192.168.10.2
      user: server2
      key_path: /private/id_ed25519
      host_key_fingerprint: SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
"""
    result = load_inventory(_write(tmp_path, text))
    assert result["tiers"]["was"]["migration_env_file"] == "/private/migrate.env"
    InventoryConfig.model_validate(result)
    _bad(tmp_path, text.replace("user: server2", "user: server2\n      password: never-print-me"))
