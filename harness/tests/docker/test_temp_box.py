"""temp-box v1 실제 소스 → 승인 → 멀티 아키텍처 빌드 → 3복제본 배포/헬스.

CodeBuild/Hub/VM/ECS는 실행하지 않는다. 빌드·smoke는 로컬 리허설 어댑터이고,
승인·실행기·CD dispatch·온프렘 provider·기록은 제품 코드를 사용한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ddak.cd.tools.deploy_tier.tool import deploy_tier
from ddak.cd.tools.rollback_tier.tool import rollback_tier
from ddak.core.config import AdapterMode
from ddak.core.contracts.base import ContractModel, ToolInput
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode, Target
from ddak.core.contracts.plan import Plan, PlanStep
from ddak.core.contracts.release import ImageArtifact, ReleaseArtifacts
from ddak.core.registry import Registry, spec_for
from ddak.core.snapshots import digest_bytes, file_manifest, preview
from ddak.executor.engine import RunStatus
from ddak.executor.service import DeploymentService
from ddak.onprem.deploy import OnPremProvider
from ddak.onprem.deploy.containers import DockerHost
from tests.docker.test_onprem_runtime import _docker, _port

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(os.environ.get("DDAK_TEST_DOCKER") != "1", reason="실 Docker opt-in 필요"),
]
APP = Path(__file__).resolve().parents[2] / "apps" / "temp-box"


class _Input(ToolInput):
    target: Target | None = None
    tier: str | None = None


class _Output(ContractModel):
    passed: bool = True
    source: str = "local-rehearsal"
    release_artifacts: ReleaseArtifacts | None = None


def _step(sid: str, tool: str, **kwargs: Any) -> PlanStep:
    spec = spec_for(tool)
    return PlanStep(id=sid, tool=tool, layer=spec.layer, effect=spec.effect, **kwargs)


def _probe(name: str, content: dict[str, str], release_id: str) -> dict:
    value = json.loads(
        _docker(
            "exec",
            name,
            "python",
            "-c",
            "import json,urllib.request as u; "
            "root='http://127.0.0.1:8000'; "
            "print(json.dumps({'html':u.urlopen(root).read().decode(),"
            "'version':json.load(u.urlopen(root+'/version')),"
            "'ready':u.urlopen(root+'/health/ready').status}))",
        )
    )
    assert content["title"] in value["html"] and content["color"] in value["html"]
    assert value["version"] == {"release_id": release_id}
    assert value["ready"] == 200
    return value


def test_temp_box_bootstrap_and_health(tmp_path: Path, monkeypatch) -> None:
    started = time.monotonic()
    original = file_manifest(APP)
    endpoint = _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    # 기존 자격증명 파일을 읽거나 복사하지 않는 공개 이미지 전용 CLI 설정.
    config = tmp_path / "docker-config"
    config.mkdir(mode=0o700)
    plugin = Path("/Applications/Docker.app/Contents/Resources/cli-plugins/docker-buildx")
    if plugin.exists():
        (config / "cli-plugins").mkdir()
        (config / "cli-plugins" / "docker-buildx").symlink_to(plugin)
    monkeypatch.setenv("DOCKER_CONFIG", str(config))
    monkeypatch.setenv("DOCKER_HOST", endpoint)
    project = "temp-box-" + uuid.uuid4().hex[:10]
    registry_name, volume, network = project + "-registry", project + "-data", project + "-net"
    name, probe_name = project + "-app", project + "-probe"
    port = _port()
    repo = f"localhost:{port}/temp-box"
    native = _docker("info", "--format", "{{.OSType}}/{{.Architecture}}")
    native = native.replace("aarch64", "arm64").replace("x86_64", "amd64")
    inventory = {
        "onprem": {
            "mode": "container",
            "tiers": {
                "was": {
                    "name": name,
                    "platform": native,
                    "network": network,
                    "ports": [],
                    "replicas": 3,
                    "ready": {},
                }
            },
        }
    }
    source = tmp_path / "source"
    shutil.copytree(APP, source)
    registry = Registry(
        spec_for(n)
        for n in ("build_image", "deploy_tier", "health_check", "smoke_test", "rollback_tier")
    )
    registry.tool("deploy_tier")(deploy_tier)
    registry.tool("rollback_tier")(rollback_tier)
    refs: list[str] = []
    tags: list[str] = []
    reports: list[dict[str, Any]] = []
    builds: dict[str, ImageArtifact] = {}
    responses: dict[str, list[dict]] = {}
    service = DeploymentService(registry, tmp_path / "state")

    @registry.tool("build_image")
    def build(inp: _Input, ctx: RunContext) -> _Output:
        assert ctx.build_source and Path(ctx.build_source).resolve() != source.resolve()
        assert all(r.decision == "approved" for r in service.store.approvals(ctx.run_id))
        tag = f"{repo}:{ctx.run_id}"
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
            ctx.build_source,
            timeout=240,
        )
        raw = _docker("buildx", "imagetools", "inspect", "--raw", tag)
        digest = digest_bytes(raw.encode())
        image = ImageArtifact(
            ref=repo + "@" + digest,
            index_digest=digest,
            platform_digests={
                f"{m['platform']['os']}/{m['platform']['architecture']}": m["digest"]
                for m in json.loads(raw)["manifests"]
            },
        )
        refs.append(image.ref)
        builds[ctx.run_id] = image
        return _Output(
            release_artifacts=ReleaseArtifacts(
                snapshot=preview(Path(ctx.build_source)), images={"was": image}
            )
        )

    @registry.tool("health_check")
    def health(inp: _Input, ctx: RunContext) -> _Output:
        return _Output(passed=OnPremProvider().health_check(ctx).passed)

    @registry.tool("smoke_test")
    def smoke(inp: _Input, ctx: RunContext) -> _Output:
        assert ctx.build_source
        content = json.loads((Path(ctx.build_source) / "content.json").read_text())
        responses[ctx.run_id] = [_probe(f"{name}-{i}", content, ctx.run_id) for i in range(1, 4)]
        return _Output()

    async def run(rid: str, mode: RunMode):
        p = Plan.model_validate(
            {
                "run_id": rid,
                "project": project,
                "mode": mode,
                "build": {
                    "steps": [_step("build.was", "build_image", tier="was")],
                    "signal": "images_ready",
                },
                "deploy": {
                    "local": {
                        "steps": [
                            _step(
                                "deploy.was.local",
                                "deploy_tier",
                                target=Target.LOCAL,
                                tier="was",
                                wait_for=["images_ready"],
                            ),
                            _step("verify.health.local", "health_check", target=Target.LOCAL),
                            _step("verify.smoke.local", "smoke_test", target=Target.LOCAL),
                        ],
                        "signal": "local_verified",
                    }
                },
            }
        )
        ctx = RunContext(
            rid, project=project, mode=mode, adapter_mode=AdapterMode.REAL, platform=inventory
        )
        begin = time.monotonic()
        service.prepare(p, ctx, source)
        approve_begin = time.monotonic()
        service.approve(rid, approver="local-rehearsal")
        approval_seconds = time.monotonic() - approve_begin
        service.start(rid)
        result = await service.wait(rid)
        reports.append(
            {
                "run_id": rid,
                "status": result.status.value,
                "local_track": result.tracks["local"].value,
                "seconds": round(time.monotonic() - begin, 3),
                "approval_seconds": round(approval_seconds, 3),
                "approval": "리허설 자동 승인; 사람 대기 시간 아님",
                "step_times": {r.tool: round(r.elapsed_s, 3) for r in result.records},
                "artifact": builds[rid].model_dump(mode="json") if rid in builds else None,
                "snapshot": service.approval_view(rid)["snapshot"],
                "observations": result.context.release_artifacts.model_dump(mode="json")[
                    "observations"
                ]
                if result.context and result.context.release_artifacts
                else {},
            }
        )
        return result

    async def scenario():
        first = await run("temp-v1", RunMode.BOOTSTRAP)
        assert first.status is RunStatus.SUCCEEDED, first
        successful = service.store.environments(project)["local"]["current"]
        assert successful["source_mode"] == "real" and successful["release_id"] == "temp-v1"
        for i in range(1, 4):
            state = DockerHost().container(f"{name}-{i}")
            assert state["Config"]["Image"] == builds["temp-v1"].ref
            assert state["State"]["Health"]["Status"] == "healthy"
            assert json.loads(
                _docker("inspect", "--format", "{{json .HostConfig.PortBindings}}", f"{name}-{i}")
            ) in ({}, None)

    proof = APP.parents[1] / "var" / "validation" / "temp-box-runtime.json"
    stop_times = {}
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
            f"type=volume,src={volume},dst=/var/lib/registry",
            "--publish",
            f"127.0.0.1:{port}:5000",
            "registry:2",
        )
        _docker("network", "create", "--label", f"ddak.project={project}", network)
        asyncio.run(scenario())
        # 두 플랫폼 모두 실제 HTTP 응답과 PID 1의 SIGTERM 종료를 확인한다.
        for platform in ("linux/amd64", "linux/arm64"):
            _docker(
                "run",
                "-d",
                "--platform",
                platform,
                "--name",
                probe_name,
                "--label",
                "ddak.managed=true",
                "--label",
                f"ddak.project={project}",
                "--label",
                "ddak.tier=was",
                "-e",
                "RELEASE_ID=platform-probe",
                builds["temp-v1"].ref,
            )
            deadline = time.monotonic() + 20
            while True:
                try:
                    _probe(
                        probe_name, {"title": "temp-box v1", "color": "#2563eb"}, "platform-probe"
                    )
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)
            before_stop = time.monotonic()
            _docker("stop", probe_name)
            stop_times[platform] = round(time.monotonic() - before_stop, 3)
            state = DockerHost().container(probe_name)
            exit_code = int(_docker("inspect", "--format", "{{.State.ExitCode}}", probe_name))
            assert exit_code == 0 and stop_times[platform] < 3
            DockerHost().remove(state, project, "was")
        assert file_manifest(APP) == original
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_text(
            json.dumps(
                {
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "source": "local-rehearsal",
                    "replicas": 3,
                    "ports": [],
                    "native_platform": native,
                    "build": "approved build_source → buildx multiarch → loopback registry",
                    "cloud": "not run",
                    "vm": "not run",
                    "dockerhub": "not run",
                    "ai": "not run",
                    "runs": reports,
                    "http": responses,
                    "sigterm_seconds": stop_times,
                    "seconds": round(time.monotonic() - started, 3),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    finally:
        service.close()
        for container in [registry_name, probe_name] + [
            f"{name}-{i}{suffix}" for i in range(1, 4) for suffix in ("", "-ddak-next")
        ]:
            with contextlib.suppress(Exception):
                host = DockerHost()
                state = host.container(container)
                if state:
                    host.remove(state, project, "registry" if container == registry_name else "was")
        with contextlib.suppress(Exception):
            _docker("network", "rm", network)
        with contextlib.suppress(Exception):
            _docker("volume", "rm", volume)
        for ref in refs + tags:
            with contextlib.suppress(Exception):
                _docker("image", "rm", ref)
