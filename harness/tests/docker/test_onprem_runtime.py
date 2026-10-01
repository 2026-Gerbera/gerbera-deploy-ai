"""실 Docker 선택 실행: DDAK_TEST_DOCKER=1 uv run pytest -m docker tests/docker -q.

Docker Desktop/local daemon + buildx + 공개 python/registry 이미지 다운로드가 필요하다.
테스트 전용 loopback registry와 multiarch HTTP 이미지를 만들고 생성 자원만 정리한다.
사용자 env/인증 파일은 읽지 않는다. registry/auth 설정은 정상 Docker CLI에 맡긴다.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.release import ImageArtifact, ReleaseArtifacts, SnapshotBinding
from ddak.onprem.deploy import OnPremProvider
from ddak.onprem.deploy.containers import DockerHost
from tests.docker.service_rehearsal import service_rehearsals

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("DDAK_TEST_DOCKER") != "1",
        reason="실 Docker 명시적 opt-in 필요",
    ),
]


def _docker(*args: str, timeout: float = 120) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        raise RuntimeError(f"test Docker 명령 실패: {' '.join(args[:2])}; exit={result.returncode}")
    return result.stdout.strip()


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http(
    port: int, expected: str, *, secret_valid: bool = True, release_id: str | None = None
) -> dict:
    until = time.monotonic() + 20
    while time.monotonic() < until:
        try:
            # 고정 loopback 테스트 URL이다.
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                value = json.load(response)
            if (
                value["version"] == expected
                and value["secret_valid"] == secret_valid
                and value["secret_present"] == secret_valid
                and (release_id is None or value["release_id"] == release_id)
                and value["app_base_url"] == f"http://localhost:{port}"
            ):
                return value
        except (OSError, urllib.error.URLError, ValueError):
            pass
        time.sleep(0.1)
    pytest.fail("실제 HTTP 버전 또는 secret 주입 확인 실패")


def test_real_http_replace_rollback_and_migration(tmp_path: Path, monkeypatch) -> None:
    started = time.monotonic()
    endpoint = _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    isolated_config = tmp_path / "docker-config"
    isolated_config.mkdir(mode=0o700)
    plugin = Path("/Applications/Docker.app/Contents/Resources/cli-plugins/docker-buildx")
    if plugin.exists():
        (isolated_config / "cli-plugins").mkdir()
        (isolated_config / "cli-plugins" / "docker-buildx").symlink_to(plugin)
    monkeypatch.setenv("DOCKER_CONFIG", str(isolated_config))
    monkeypatch.setenv("DOCKER_HOST", endpoint)
    project = "ddak-o1-" + uuid.uuid4().hex[:10]
    registry_name, app_name = project + "-registry", project + "-was"
    volume_name = project + "-data"
    registry_volume = project + "-registry-data"
    network_name = project + "-net"
    registry_port, http_port = _port(), _port()
    repo = f"localhost:{registry_port}/fixture"
    platform = _docker("info", "--format", "{{.OSType}}/{{.Architecture}}")
    platform = platform.replace("aarch64", "arm64").replace("x86_64", "amd64")
    tags = []
    refs = []
    artifacts = []
    try:
        _docker(
            "run",
            "-d",
            "--name",
            registry_name,
            "--label",
            "ddak.managed=true",
            "--label",
            f"ddak.project={project}",
            "--label",
            "ddak.tier=registry",
            "--mount",
            f"type=volume,src={registry_volume},dst=/var/lib/registry",
            "--publish",
            f"127.0.0.1:{registry_port}:5000",
            "registry:2",
        )
        for version in ("v1", "v2", "broken"):
            tag = repo + ":" + version
            tags.append(tag)
            _docker(
                "buildx",
                "build",
                "--platform",
                "linux/amd64,linux/arm64",
                "--provenance=false",
                "--push",
                "--tag",
                tag,
                "--build-arg",
                "VERSION=" + version,
                "--build-arg",
                "READY=" + ("fail" if version == "broken" else "ok"),
                str(Path(__file__).parent / "fixture"),
                timeout=240,
            )
            raw = _docker("buildx", "imagetools", "inspect", "--raw", tag)
            index_digest = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
            manifest = json.loads(raw)
            children = {
                f"{entry['platform']['os']}/{entry['platform']['architecture']}": entry["digest"]
                for entry in manifest["manifests"]
            }
            ref = repo + "@" + index_digest
            refs.append(ref)
            artifacts.append(
                ImageArtifact(ref=ref, index_digest=index_digest, platform_digests=children)
            )
        image_preparation_seconds = time.monotonic() - started
        source_hash = "sha256:" + hashlib.sha256(b"runtime-fixture").hexdigest()
        ctx = RunContext(
            project,
            project=project,
            adapter_mode=AdapterMode.REAL,
            deadline=time.monotonic() + 180,
            images={"was": refs[0]},
            platform={
                "onprem": {
                    "tiers": {
                        "was": {
                            "name": app_name,
                            "platform": platform,
                            "ports": [f"127.0.0.1:{http_port}:8000"],
                            "volumes": [{"name": volume_name, "target": "/data"}],
                            "env_file": str(tmp_path / "was.env"),
                            "public_env": {
                                "APP_BASE_URL": f"http://localhost:{http_port}",
                                "SESSION_COOKIE_SECURE": "false",
                                "DB_HOST": "fixture-db",
                                "DB_NAME": "fixture",
                            },
                        }
                    }
                }
            },
            release_artifacts=ReleaseArtifacts(
                snapshot=SnapshotBinding(
                    source_snapshot_hash=source_hash, build_snapshot_hash=source_hash
                ),
                images={"was": artifacts[0]},
            ),
        )
        provider = OnPremProvider()
        assert provider.inject_config(["SECRET_KEY"], ctx).changed
        assert provider.inject_config(["SECRET_KEY"], ctx).changed is False
        first = provider.deploy("was", ctx)
        responses = [_http(http_port, "v1")]
        ctx = replace(
            ctx,
            images={"was": refs[1]},
            previous_release={"local": {"release_id": "v1", "images": {"was": refs[0]}}},
            release_artifacts=ctx.release_artifacts.model_copy(
                update={
                    "images": {
                        "was": artifacts[1],
                    }
                }
            ),
        )
        migrated = provider.migrate_db(["001_fixture"], ctx)
        assert migrated.changed
        assert provider.migrate_db(["001_fixture"], ctx).changed is False
        second = provider.deploy("was", ctx)
        responses.append(_http(http_port, "v2"))
        rollback = provider.rollback("was", ctx)
        responses.append(_http(http_port, "v1"))
        repeated = provider.rollback("was", ctx)
        assert repeated.changed is False
        responses.append(_http(http_port, "v1"))
        for result, artifact in (
            (first, artifacts[0]),
            (second, artifacts[1]),
            (rollback, artifacts[0]),
        ):
            assert result.observation.platform_digest == artifact.platform_digests[platform]
            assert result.observation.platform_digest != artifact.index_digest
        running = DockerHost().container(app_name)
        assert running["ImageManifestDescriptor"]["digest"] == rollback.observation.platform_digest
        provider_elapsed_seconds = time.monotonic() - started - image_preparation_seconds
        _docker("network", "create", "--label", f"ddak.project={project}", network_name)
        replica_inventory = copy.deepcopy(ctx.platform)
        replica_inventory["onprem"]["tiers"]["was"].update(
            {
                "name": app_name + "-replica",
                "network": network_name,
                "ports": [],
                "replicas": 3,
                "ready": {"timeout_s": 2.0},
                "traefik_labels": {"traefik.enable": "true"},
            }
        )
        replica_ctx = replace(
            ctx,
            platform=replica_inventory,
            deadline=time.monotonic() + 180,
            images={"was": refs[0]},
            release_artifacts=None,
        )
        provider.deploy("was", replica_ctx)
        assert provider.health_check(replica_ctx).passed
        replica_ctx = replace(replica_ctx, images={"was": refs[1]})
        provider.deploy("was", replica_ctx)
        assert provider.health_check(replica_ctx).passed
        before_broken = {
            i: DockerHost().container(app_name + f"-replica-{i}")["Id"] for i in range(1, 4)
        }
        replica_ctx = replace(
            replica_ctx,
            run_id="replica-broken",
            images={"was": refs[2]},
            previous_release={
                "local": {"release_id": replica_ctx.run_id, "images": {"was": refs[1]}}
            },
        )
        with pytest.raises(DdakToolError) as ready_error:
            provider.deploy("was", replica_ctx)
        assert ready_error.value.code == ErrorCode.ADAPTER_FAILED
        replica_rollback = provider.rollback("was", replica_ctx)
        assert replica_rollback.detail == "복구 replica: [1]"
        assert not provider.rollback("was", replica_ctx).changed
        for i in range(1, 4):
            observed = DockerHost().container(app_name + f"-replica-{i}")
            assert observed["Config"]["Image"] == refs[1]
            assert (
                observed["ImageManifestDescriptor"]["digest"]
                == artifacts[1].platform_digests[platform]
            )
            if i > 1:
                assert observed["Id"] == before_broken[i]
        service_port = _port()
        service_inventory = copy.deepcopy(ctx.platform)
        service_inventory["onprem"]["tiers"]["was"].update(
            {
                "name": app_name + "-service",
                "ports": [],
                "replicas": 3,
                "network": network_name,
                "env_file": str(tmp_path / "service.env"),
                "ready": {"timeout_s": 2.0},
                "public_env": {"APP_BASE_URL": f"http://localhost:{service_port}"},
            }
        )
        service_ctx = replace(ctx, project="flaskr", platform=service_inventory)

        def replica_http(port, expected, *, secret_valid=True, release_id=None):
            values = []
            for i in range(1, 4):
                name = app_name + f"-service-{i}"
                observed = DockerHost().container(name)
                assert observed["State"]["Health"]["Status"] == "healthy"
                assert json.loads(
                    _docker(
                        "container",
                        "inspect",
                        "--format",
                        "{{json .HostConfig.PortBindings}}",
                        name,
                    )
                ) in ({}, None)
                value = json.loads(
                    _docker(
                        "container",
                        "exec",
                        name,
                        "python",
                        "-c",
                        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/version').read().decode())",
                    )
                )
                assert value["version"] == expected and value["secret_valid"] == secret_valid
                assert value["secret_present"] == secret_valid and value["release_id"] == release_id
                values.append(value)
            return {"replicas": values}

        rehearsals = service_rehearsals(
            tmp_path / "service", service_ctx, artifacts, service_port, replica_http
        )
        proof = {
            "source": "real-docker-test-runtime",
            "fixture": "tests/docker/fixture",
            "completed_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "platform": platform,
            "image_preparation_seconds": round(image_preparation_seconds, 2),
            "provider_seconds": round(provider_elapsed_seconds, 2),
            "service_rehearsals": rehearsals,
            "http_responses": responses,
            "replica_ready_failure": ready_error.value.code.value,
            "replica_rollback": replica_rollback.model_dump(mode="json"),
            "releases": [result.model_dump(mode="json") for result in (first, second, rollback)],
            "repeated_rollback_changed": repeated.changed,
            "migration": migrated.migration,
            "docker_image_id": running["Image"],
            "native_platform_descriptor": running["ImageManifestDescriptor"],
        }
        (Path(__file__).parents[2] / "var" / "validation").mkdir(parents=True, exist_ok=True)
        (Path(__file__).parents[2] / "var" / "validation" / "onprem-runtime.json").write_text(
            json.dumps(proof, indent=2, ensure_ascii=False) + "\n",
        )
    finally:
        replica_names = [
            app_name + f"-replica-{i}" + suffix
            for i in range(1, 4)
            for suffix in ("", "-ddak-next")
        ]
        for name in (
            app_name,
            app_name + "-ddak-next",
            app_name + "-ddak-migrate",
            registry_name,
            *replica_names,
        ):
            with contextlib.suppress(Exception):
                host = DockerHost()
                state = host.container(name)
                if state:
                    tier = "registry" if name == registry_name else "was"
                    host.remove(state, project, tier)
        with contextlib.suppress(Exception):
            _docker("network", "rm", network_name)
        for volume in (volume_name, registry_volume):
            with contextlib.suppress(Exception):
                _docker("volume", "rm", volume)
        for ref in refs + tags:
            with contextlib.suppress(Exception):
                _docker("image", "rm", ref)
