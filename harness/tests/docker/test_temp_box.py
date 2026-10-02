"""실 Docker: flaskr WAS/SQLite 먼저, 이어 DB→WAS 3개→WEB/MySQL. VM 접속 없음."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from pathlib import Path

import pytest

from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.release import ReleaseArtifacts
from ddak.core.snapshots import preview
from ddak.executor.engine import RunStatus
from ddak.executor.service import DeploymentService
from ddak.onprem.deploy import OnPremProvider, preflight_inventory
from ddak.onprem.deploy.containers import DockerHost
from tests.docker.test_onprem_runtime import _docker, _port
from tests.support import load_script

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(os.environ.get("DDAK_TEST_DOCKER") != "1", reason="실 Docker opt-in 필요"),
]
APP = Path(__file__).resolve().parents[2] / "apps" / "temp-box"
cli = load_script("o1_onprem")


def test_flaskr_was_then_three_tier(tmp_path, monkeypatch):
    started = time.monotonic()
    endpoint = _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    config = tmp_path / "docker-config"
    config.mkdir(mode=0o700)
    plugin = Path("/Applications/Docker.app/Contents/Resources/cli-plugins/docker-buildx")
    if plugin.exists():
        (config / "cli-plugins").mkdir()
        (config / "cli-plugins" / "docker-buildx").symlink_to(plugin)
    monkeypatch.setenv("DOCKER_CONFIG", str(config))
    monkeypatch.setenv("DOCKER_HOST", endpoint)
    project = "flaskr-test-" + uuid.uuid4().hex[:8]
    network, registry_name = project + "-net", project + "-registry"
    registry_volume = project + "-registry-data"
    registry_port = _port()
    source = tmp_path / "source"
    from ddak.core.snapshots import copy_source

    copy_source(APP, source)
    locked = json.loads((APP / "images.lock.json").read_text())
    native = _docker("info", "--format", "{{.OSType}}/{{.Architecture}}")
    native = native.replace("aarch64", "arm64").replace("x86_64", "amd64")
    resources: list[tuple[str, str, str]] = []
    volumes = [registry_volume]
    results = []
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
        resources.append((registry_name, project, "registry"))
        _docker("network", "create", "--label", f"ddak.project={project}", network)
        artifact_file = tmp_path / "images.json"
        cli.build(source, f"localhost:{registry_port}/flaskr", artifact_file, three=True)
        built = ReleaseArtifacts.model_validate(json.loads(artifact_file.read_text())["artifacts"])
        assert built.snapshot == preview(source)
        for layout in ("was", "three"):
            run_project = project + "-" + layout
            directory = tmp_path / layout
            port = _port()
            base_url = f"http://127.0.0.1:{port}"
            cli.initialize(directory, layout, base_url, native)
            inv = json.loads((directory / "inventory.json").read_text())
            inv["mode"] = "container"
            was = inv["tiers"]["was"]
            was_name = run_project + "-was"
            db_name = run_project + "-db"
            web_name = run_project + "-web"
            router = run_project + "-router"
            was["name"] = was_name
            was["traefik_labels"] = {
                k.replace("flaskr-was-was", was_name).replace(
                    "flaskr-three-was", was_name
                ): v.replace("flaskr-was-was", was_name).replace("flaskr-three-was", was_name)
                for k, v in was["traefik_labels"].items()
            }
            for cfg in inv["tiers"].values():
                cfg.pop("ssh")
                cfg["network"] = network
                for vol in cfg.get("volumes", []):
                    vol["name"] = run_project + "-data"
                    volumes.append(vol["name"])
            for number in range(1, 4):
                resources.extend(
                    [
                        (f"{was_name}-{number}{suffix}", run_project, "was")
                        for suffix in ("", "-ddak-next")
                    ]
                )
            resources.append((was_name + "-ddak-migrate", run_project, "was"))
            resources.append((router, run_project, "router"))
            router_args = ["--publish", f"127.0.0.1:{port}:8080"] if layout == "was" else []
            _docker(
                "run",
                "-d",
                "--name",
                router,
                "--network",
                network,
                "--label",
                "ddak.managed=true",
                "--label",
                f"ddak.project={run_project}",
                "--label",
                "ddak.tier=router",
                "--mount",
                "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock,readonly",
                *router_args,
                locked["traefik"]["ref"],
                "--providers.docker=true",
                "--providers.docker.exposedbydefault=false",
                "--providers.docker.constraints=Label(`ddak.project`,`" + run_project + "`)",
                "--entrypoints.web.address=:8080",
                "--providers.providersThrottleDuration=0.1s",
            )
            if layout == "three":
                db = inv["tiers"]["db"]
                db["name"], db["ports"] = db_name, []
                inv["tiers"]["web"].update(name=web_name, ports=[f"127.0.0.1:{port}:8080"])
                inv["tiers"]["web"]["public_env"]["WAS_UPSTREAM"] = router + ":8080"
                for filename in ("was.env", "migrate.env"):
                    path = directory / "private" / filename
                    path.write_text(path.read_text().replace("192.168.10.3", db_name))
                resources += [
                    (db_name, run_project, "db"),
                    (web_name, run_project, "web"),
                    (web_name + "-ddak-next", run_project, "web"),
                ]
            artifacts = built.model_copy(
                update={"images": {k: v for k, v in built.images.items() if k in inv["tiers"]}}
            )
            provider = OnPremProvider()
            check = preflight_inventory(inv, project=run_project)
            assert check["passed"], check
            with contextlib.closing(
                DeploymentService(cli.registry_for(provider), directory / "state")
            ) as service:

                async def run(
                    release,
                    mode,
                    *,
                    layout=layout,
                    run_project=run_project,
                    inv=inv,
                    artifacts=artifacts,
                    check=check,
                ):
                    plan = cli.deployment_plan(release, run_project, mode, three=layout == "three")
                    ctx = RunContext(
                        release,
                        project=run_project,
                        mode=mode,
                        adapter_mode=AdapterMode.REAL,
                        platform={"onprem": inv},
                        images={k: v.ref for k, v in artifacts.images.items()},
                        release_artifacts=artifacts,
                    )
                    service.prepare(plan, ctx, source)
                    service.approve(release, approver="local-docker-rehearsal")
                    service.start(release)
                    start = time.monotonic()
                    result = await service.wait(release)
                    report = {
                        "layout": layout,
                        "run_id": release,
                        "status": result.status.value,
                        "seconds": round(time.monotonic() - start, 3),
                        "steps": [
                            {"tool": r.tool, "status": r.status, "error": r.error}
                            for r in result.records
                        ],
                        "preflight": check,
                    }
                    results.append(report)
                    assert result.status is RunStatus.SUCCEEDED, report
                    return result.context

                ctx = asyncio.run(run(layout + "-v1", RunMode.BOOTSTRAP))
                assert ctx
                ids = {
                    tier: DockerHost().container(cfg["name"])["Id"]
                    for tier, cfg in inv["tiers"].items()
                    if tier != "was"
                }
                if layout == "three":
                    ctx = asyncio.run(run(layout + "-v2", RunMode.UPDATE))
                    assert {
                        tier: DockerHost().container(inv["tiers"][tier]["name"])["Id"]
                        for tier in ids
                    } == ids
                    # DB·web ID는 유지하고 WAS만 새 릴리스로 교체한다.
                for i in range(1, 4):
                    state = DockerHost().container(f"{was_name}-{i}")
                    assert state["State"]["Health"]["Status"] == "healthy"
                    assert json.loads(
                        _docker(
                            "inspect", "--format", "{{json .HostConfig.PortBindings}}", state["Id"]
                        )
                    ) in ({}, None)
                before = time.monotonic()
                _docker("stop", "--time", "30", f"{was_name}-3")
                results[-1]["sigterm_s"] = round(time.monotonic() - before, 3)
                assert _docker("inspect", "--format", "{{.State.ExitCode}}", f"{was_name}-3") == "0"
            # 다음 layout이 같은 Traefik 네트워크에 있어도 서비스 constraint로 분리된다.
    finally:
        proof = APP.parents[1] / "var" / "validation" / "flaskr-three-tier-runtime.json"
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_text(
            json.dumps(
                {
                    "source": "real-local-docker",
                    "native": native,
                    "replicas": 3,
                    "ports": [],
                    "vm": "SKIPPED: 사용자 요청, 접속 없음",
                    "hub": "not used",
                    "seconds": round(time.monotonic() - started, 3),
                    "runs": results,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        for name, owner, tier in reversed(resources):
            with contextlib.suppress(Exception):
                host = DockerHost()
                current = host.container(name)
                if current:
                    host.remove(current, owner, tier)
        with contextlib.suppress(Exception):
            _docker("network", "rm", network)
        # 이 함수에서 만든 무작위 이름의 격리된 테스트 볼륨만 정리. 운영 reset 경로가 아니다.
        for volume in volumes:
            with contextlib.suppress(Exception):
                _docker("volume", "rm", volume)
