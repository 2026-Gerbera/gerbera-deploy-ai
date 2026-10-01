"""onprem/inventory: platform.onprem.yaml 읽기·검증."""

from __future__ import annotations

from pathlib import Path

import pytest

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.onprem.deploy.provider import _Inventory as ProviderInventory
from ddak.onprem.deploy.provider import _Tier as ProviderTier
from ddak.onprem.inventory import _Tier as OurTier  # pyright: ignore[reportPrivateUsage]
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
    ProviderInventory.model_validate(result)  # O1 입력 모델이 받아들인다


def test_minimal_inventory_without_docker_host(tmp_path: Path) -> None:
    text = "tiers:\n  db: {name: demo-db, platform: linux/amd64}\n"
    result = load_inventory(_write(tmp_path, text))
    assert result["docker_host"] is None
    ProviderInventory.model_validate(result)


def test_tier_fields_match_provider() -> None:
    assert OurTier.model_fields.keys() == ProviderTier.model_fields.keys()


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
