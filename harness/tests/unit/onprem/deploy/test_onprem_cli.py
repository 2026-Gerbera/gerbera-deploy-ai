"""운영 CLI의 오프라인 준비·소스/이미지 바인딩·배포 순서."""

import json
from pathlib import Path

import pytest

from ddak.core.contracts.enums import RunMode
from ddak.core.snapshots import digest_bytes
from tests.support import load_script

cli = load_script("o1_onprem")


def test_initialization_keeps_credentials_separate_and_refuses_regeneration(tmp_path):
    directory = tmp_path / "three"
    report = cli.initialize(directory, "three", "https://app.example.test", "linux/amd64")
    inv = json.loads((directory / "inventory.json").read_text())
    assert [(k, v["ssh"]["host"], v["ssh"]["user"]) for k, v in inv["tiers"].items()] == [
        ("was", "192.168.10.2", "server2"),
        ("db", "192.168.10.3", "server3"),
        ("web", "192.168.10.4", "server1"),
    ]
    envs = {
        p.stem: dict(line.split("=", 1) for line in p.read_text().splitlines())
        for p in (directory / "private").glob("*.env")
    }
    assert "DATABASE_URL_MIGRATOR" not in envs["was"] and "MYSQL_ROOT_PASSWORD" not in envs["was"]
    assert envs["migrate"]["DATABASE_URL"] == envs["migrate"]["DATABASE_URL_MIGRATOR"]
    assert (
        len(
            {
                envs["db"][k]
                for k in ("MYSQL_ROOT_PASSWORD", "DDAK_APP_PASSWORD", "DDAK_MIGRATION_PASSWORD")
            }
        )
        == 3
    )
    assert envs["db"]["DDAK_APP_PASSWORD"] in envs["was"]["DATABASE_URL"]
    assert envs["db"]["DDAK_MIGRATION_PASSWORD"] in envs["migrate"]["DATABASE_URL_MIGRATOR"]
    assert inv["tiers"]["web"]["public_env"]["APP_BASE_URL"] == inv["public_url"]
    before = {p: p.read_bytes() for p in (directory / "private").glob("*")}
    for p in before:
        assert p.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        cli.initialize(directory, "three", "https://app.example.test", "linux/amd64")
    assert before == {p: p.read_bytes() for p in before}
    assert not any(
        secret in json.dumps(report) for secret in envs["db"].values() if len(secret) == 64
    )


def test_was_sqlite_state_is_separate(tmp_path):
    cli.initialize(tmp_path / "was", "was", "http://192.168.10.2:8080", "linux/amd64")
    inv = json.loads((tmp_path / "was" / "inventory.json").read_text())
    assert set(inv["tiers"]) == {"was"}
    volumes = inv["tiers"]["was"]["volumes"]
    assert volumes == [
        {"name": "flaskr-was-sqlite", "target": "/data"},
        {"name": "flaskr-was-uploads", "target": "/app/img"},
    ]
    assert not (tmp_path / "was" / "private" / "db.env").exists()


def test_plan_orders_database_before_migration_and_was_before_web():
    plan = cli.deployment_plan("v1", "flaskr-three", RunMode.BOOTSTRAP, three=True)
    assert [(s.tool, s.tier) for s in plan.deploy.local.steps] == [
        ("inject_env_config", None),
        ("deploy_tier", "db"),
        ("prepare_db", None),
        ("deploy_tier", "was"),
        ("deploy_tier", "web"),
        ("health_check", None),
        ("smoke_test", None),
    ]
    assert not plan.deploy.cloud.steps


def test_index_reference_retains_registry_port_and_manifest_hash(monkeypatch):
    raw = json.dumps(
        {
            "manifests": [
                {"platform": {"os": "linux", "architecture": a}, "digest": "sha256:" + c * 64}
                for a, c in (("amd64", "a"), ("arm64", "b"))
            ]
        }
    )
    ref = "localhost:5999/flaskr-was@" + digest_bytes(raw.encode())
    monkeypatch.setattr(cli, "docker", lambda *args: raw + "\n")
    assert cli.inspect_artifact(ref).ref == ref
    with pytest.raises(ValueError, match="digest"):
        cli.inspect_artifact("localhost:5999/flaskr-was@sha256:" + "f" * 64)


def test_web_inputs_ignores_python_changes_but_detects_static_and_config(tmp_path):
    (tmp_path / "flaskr" / "static").mkdir(parents=True)
    (tmp_path / "docker" / "nginx").mkdir(parents=True)
    (tmp_path / "flaskr" / "blog.py").write_text("v1")
    path = tmp_path / "flaskr" / "static" / "style.css"
    path.write_text("v1")
    initial = cli.web_inputs(tmp_path)
    (tmp_path / "flaskr" / "blog.py").write_text("v2")
    assert cli.web_inputs(tmp_path) == initial
    path.write_text("v2")
    assert cli.web_inputs(tmp_path) != initial
    updated = cli.web_inputs(tmp_path)
    (tmp_path / "docker" / "nginx" / "env.sh").write_text("v2")
    assert cli.web_inputs(tmp_path) != updated


def test_cli_declined_approval_performs_no_deployment(tmp_path, monkeypatch):
    import asyncio

    from ddak.core.contracts.release import ImageArtifact, ReleaseArtifacts
    from ddak.core.snapshots import preview

    directory = tmp_path / "was"
    cli.initialize(directory, "was", "http://127.0.0.1:8080", "linux/amd64")
    inv = json.loads((directory / "inventory.json").read_text())
    inv["mode"] = "container"
    inv["tiers"]["was"].pop("ssh")
    (directory / "inventory.json").write_text(json.dumps(inv))
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("source")
    image = ImageArtifact(
        ref="example.invalid/was@sha256:" + "a" * 64,
        index_digest="sha256:" + "a" * 64,
        platform_digests={"linux/amd64": "sha256:" + "b" * 64, "linux/arm64": "sha256:" + "c" * 64},
    )
    artifact_file = tmp_path / "images.json"
    artifact_file.write_text(
        json.dumps(
            {
                "artifacts": ReleaseArtifacts(
                    snapshot=preview(source), images={"was": image}
                ).model_dump(mode="json")
            }
        )
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    monkeypatch.setattr(cli, "docker", lambda *args: pytest.fail("승인 거부 뒤 Docker 호출"))
    assert asyncio.run(cli.deploy(directory, source, artifact_file, RunMode.BOOTSTRAP)) == {
        "status": "DENIED"
    }


@pytest.mark.parametrize("layout", ["was", "three"])
def test_generated_vm_inventory_validates_after_operator_fills_ssh(tmp_path, layout):
    from ddak.onprem.deploy.provider import _Inventory
    from tests.unit.onprem.deploy.test_onprem_vm import PIN

    directory = tmp_path / layout
    cli.initialize(directory, layout, "https://app.example.test", "linux/amd64")
    inv = json.loads((directory / "inventory.json").read_text())
    for tier in inv["tiers"].values():
        tier["ssh"]["host_key_fingerprint"] = PIN
    assert _Inventory.model_validate(inv).mode == "vm"


@pytest.mark.parametrize(
    "public_env",
    [
        {"PUBLIC_HOST": "bad_name"},
        {"WAS_UPSTREAM": "bad_name:8080"},
        {"TRUSTED_PROXY_CIDR": "172.30.10.0/24"},
        {"TRUSTED_PROXY_CIDR": "172.30.10.2"},
        {"WAS_UPSTREAM": "was:99999"},
    ],
)
def test_nginx_unsupported_inputs_are_rejected_before_deploy(public_env):
    from ddak.onprem.deploy.provider import _Tier

    with pytest.raises(ValueError):
        _Tier.valid_public_env(public_env)


def test_web_build_label_does_not_depend_on_was_source(tmp_path, monkeypatch):
    from ddak.core.contracts.release import ImageArtifact

    source = tmp_path / "source"
    (source / "flaskr" / "static").mkdir(parents=True)
    (source / "flaskr" / "blog.py").write_text("v1")
    (source / "flaskr" / "static" / "style.css").write_text("same")
    (source / "images.lock.json").write_text((cli.APP / "images.lock.json").read_text())
    labels = []

    def docker(*args):
        labels.append(args[args.index("--label") + 1])
        Path(args[args.index("--metadata-file") + 1]).write_text(
            json.dumps({"containerimage.digest": "sha256:" + "a" * 64})
        )
        return ""

    def inspect(ref):
        return ImageArtifact.model_validate(
            {
                "ref": ref,
                "index_digest": "sha256:" + "a" * 64,
                "platform_digests": {
                    "linux/amd64": "sha256:" + "b" * 64,
                    "linux/arm64": "sha256:" + "c" * 64,
                },
            }
        )

    monkeypatch.setattr(cli, "docker", docker)
    monkeypatch.setattr(cli, "inspect_artifact", inspect)
    cli.build(source, "example/flaskr", tmp_path / "v1.json", three=True)
    (source / "flaskr" / "blog.py").write_text("v2")
    cli.build(source, "example/flaskr", tmp_path / "v2.json", three=True)
    assert labels[0] != labels[2] and labels[1] == labels[3]
