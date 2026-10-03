#!/usr/bin/env python3
"""WSL2 온프렘 최초 배포/업데이트. init은 오프라인, build만 registry에 push한다."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import subprocess
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ddak.cd.tools.deploy_tier.tool import deploy_tier
from ddak.cd.tools.inject_env_config.tool import inject_env_config
from ddak.cd.tools.prepare_db.tool import prepare_db
from ddak.cd.tools.rollback_tier.tool import rollback_tier
from ddak.core.config import AdapterMode
from ddak.core.contracts.base import ContractModel, ToolInput
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode, Target
from ddak.core.contracts.plan import Plan, PlanStep
from ddak.core.contracts.release import ImageArtifact, ReleaseArtifacts
from ddak.core.registry import Registry, spec_for
from ddak.core.snapshots import copy_source, digest_bytes, digest_json, file_manifest, preview
from ddak.executor.service import DeploymentService
from ddak.onprem.deploy import OnPremProvider, preflight_inventory
from ddak.onprem.deploy.provider import _Inventory, _Tier

APP = Path(__file__).resolve().parents[1] / "apps" / "temp-box"


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def initialize(directory: Path, layout: str, base_url: str, platform: str) -> dict:
    """새 시험 환경만. 부분 생성/분실한 env를 자동 재발급하지 않는다."""
    _Tier.valid_public_env({"APP_BASE_URL": base_url})
    origin = urlsplit(base_url)
    if origin.path not in {"", "/"}:
        raise ValueError("APP_BASE_URL은 경로 없는 origin이어야 한다")
    directory = directory.absolute()
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ValueError("상태 경로 심볼릭 링크 금지")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    project = "flaskr-was" if layout == "was" else "flaskr-three"
    private = directory / "private"
    private.mkdir(mode=0o700)
    public = {
        "APP_BASE_URL": base_url.rstrip("/"),
        "SESSION_COOKIE_SECURE": str(origin.scheme == "https").lower(),
        "PROXY_FIX_X_FOR": "0" if layout == "was" else "2",
        "PROXY_FIX_X_PROTO": "0" if layout == "was" else "1",
    }
    was = {"SECRET_KEY": secrets.token_hex(32), **public}
    migration: dict[str, str] = {}
    if layout == "was":
        was["DATABASE_URL"] = "sqlite:////data/flaskr.sqlite"
        migration["DATABASE_URL"] = was["DATABASE_URL"]
    else:
        app_password, migration_password = secrets.token_hex(32), secrets.token_hex(32)
        was["DATABASE_URL"] = (
            f"mysql+pymysql://flaskr_app:{app_password}@192.168.10.3:3306/flaskr?charset=utf8mb4"
        )
        migration["DATABASE_URL_MIGRATOR"] = (
            f"mysql+pymysql://flaskr_migrator:{migration_password}@192.168.10.3:3306/flaskr?charset=utf8mb4"
        )
        migration["DATABASE_URL"] = migration["DATABASE_URL_MIGRATOR"]
        db = {
            "MYSQL_DATABASE": "flaskr",
            "MYSQL_ROOT_PASSWORD": secrets.token_hex(32),
            "DDAK_APP_PASSWORD": app_password,
            "DDAK_MIGRATION_PASSWORD": migration_password,
        }
        write_private(private / "db.env", "".join(f"{k}={v}\n" for k, v in db.items()))
        write_private(private / "web.env", "")
    for name, values in (("was", was), ("migrate", migration)):
        write_private(private / f"{name}.env", "".join(f"{k}={v}\n" for k, v in values.items()))

    def ssh(host: str, user: str) -> dict:
        return {
            "host": host,
            "user": user,
            "key_path": f"/home/REPLACE/.ssh/{project}",
            "host_key_fingerprint": "REPLACE_WITH_CONSOLE_VERIFIED_SHA256",
        }

    service = project + "-was"
    tiers = {
        "was": {
            "name": service,
            "kind": "python_http",
            "platform": platform,
            "ssh": ssh("192.168.10.2", "server2"),
            "network": project + "-was-net",
            "env_file": str(private / "was.env"),
            "migration_env_file": str(private / "migrate.env"),
            "public_env": public,
            "replicas": 3,
            "ports": [],
            "ready": {"timeout_s": 30},
            "volumes": [
                *([{"name": project + "-sqlite", "target": "/data"}] if layout == "was" else []),
                {"name": project + "-uploads", "target": "/app/img"},
            ],
            "traefik_labels": {
                "traefik.enable": "true",
                f"traefik.http.routers.{service}.rule": "PathPrefix(`/`)",
                f"traefik.http.routers.{service}.entrypoints": "web",
                f"traefik.http.routers.{service}.service": service,
                f"traefik.http.services.{service}.loadbalancer.server.port": "8000",
            },
        }
    }
    if layout == "three":
        tiers["db"] = {
            "name": project + "-db",
            "kind": "mysql",
            "platform": platform,
            "ssh": ssh("192.168.10.3", "server3"),
            "network": project + "-db-net",
            "env_file": str(private / "db.env"),
            "ports": ["192.168.10.3:3306:3306"],
            "volumes": [{"name": project + "-mysql", "target": "/var/lib/mysql"}],
            "ready": {"timeout_s": 120},
        }
        tiers["web"] = {
            "name": project + "-web",
            "kind": "nginx",
            "platform": platform,
            "ssh": ssh("192.168.10.4", "server1"),
            "network": project + "-web-net",
            "env_file": str(private / "web.env"),
            "ports": ["192.168.10.4:8080:8080"],
            "ready": {"port": 8080, "timeout_s": 30},
            "public_env": {
                "APP_BASE_URL": base_url.rstrip("/"),
                "WAS_UPSTREAM": "192.168.10.2:8080",
                "PUBLIC_HOST": origin.netloc,
                "PUBLIC_SCHEME": origin.scheme,
                "TRUSTED_PROXY_CIDR": "172.30.10.2/32",
            },
        }
    inventory = {"mode": "vm", "public_url": base_url.rstrip("/"), "tiers": tiers}
    write_private(directory / "inventory.json", json.dumps(inventory, indent=2) + "\n")
    write_private(
        directory / "project.json", json.dumps({"project": project, "layout": layout}) + "\n"
    )
    return {
        "created": str(directory),
        "project": project,
        "inventory": "SSH 지문·키 경로 수정 필요",
        "credentials": "private/ 보존; 값은 출력하지 않음",
    }


def docker(*args: str, timeout: int = 600) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        raise ValueError("Docker 명령 실패; 인증/빌드 설정 확인 (원문 로그·자격증명 출력 생략)")
    return result.stdout


def inspect_artifact(ref: str) -> ImageArtifact:
    raw = docker("buildx", "imagetools", "inspect", "--raw", ref)
    index = ref.rsplit("@", 1)[1]
    if index not in {digest_bytes(raw.encode()), digest_bytes(raw.removesuffix("\n").encode())}:
        raise ValueError("registry index digest 불일치")
    manifests = json.loads(raw).get("manifests", [])
    platforms = {
        f"{m['platform']['os']}/{m['platform']['architecture']}": m["digest"]
        for m in manifests
        if m.get("platform", {}).get("architecture") in {"amd64", "arm64"}
    }
    repo = ref.split("@", 1)[0]
    return ImageArtifact.model_validate(
        {"ref": f"{repo}@{index}", "index_digest": index, "platform_digests": platforms}
    )


def web_inputs(source: Path) -> str:
    manifest = file_manifest(source)
    return digest_json(
        {
            p: v
            for p, v in manifest.items()
            if p in {"docker/web.Dockerfile", "LICENSE.txt", "NOTICE.md"}
            or p.startswith(("docker/nginx/", "docker/web/", "flaskr/static/"))
        }
    )


def build(
    source: Path, repository: str, output: Path, *, three: bool, reuse_web: Path | None = None
) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]*", repository) or "@" in repository:
        raise ValueError("repository prefix 형식 오류")
    snapshot = preview(source)
    with tempfile.TemporaryDirectory(prefix="ddak-build-") as temporary:
        copied = Path(temporary) / "source"
        copy_source(source, copied)
        if preview(copied) != snapshot:
            raise ValueError("복사 중 소스 변경")
        images = {}
        previous = json.loads(reuse_web.read_text()) if reuse_web else None
        for tier in ["was", "web"] if three else ["was"]:
            if tier == "web" and previous and previous.get("web_inputs") == web_inputs(copied):
                images[tier] = ImageArtifact.model_validate(previous["artifacts"]["images"]["web"])
                continue
            tag = repository + f"-{tier}:" + uuid.uuid4().hex
            metadata = Path(temporary) / f"{tier}-metadata.json"
            docker(
                "buildx",
                "build",
                "--platform",
                "linux/amd64,linux/arm64",
                "--provenance=false",
                "--label",
                "ddak.source-sha256="
                + (web_inputs(copied) if tier == "web" else snapshot.build_snapshot_hash),
                "--metadata-file",
                str(metadata),
                "--push",
                "--tag",
                tag,
                "--file",
                str(copied / "docker" / f"{tier}.Dockerfile"),
                str(copied),
            )
            index = json.loads(metadata.read_text())["containerimage.digest"]
            images[tier] = inspect_artifact(repository + f"-{tier}@" + index)
        if three:
            locked = json.loads((copied / "images.lock.json").read_text())["mysql"]
            images["db"] = ImageArtifact.model_validate(
                {k: locked[k] for k in ("ref", "index_digest", "platform_digests")}
            )
        value = {
            "artifacts": ReleaseArtifacts(snapshot=snapshot, images=images).model_dump(mode="json"),
            "web_inputs": web_inputs(copied),
        }
        write_private(output, json.dumps(value, indent=2) + "\n")


class HttpInput(ToolInput):
    target: Target


class HttpOutput(ContractModel):
    passed: bool
    source: str = "real-onprem-cli"


def registry_for(provider: OnPremProvider) -> Registry:
    registry = Registry(
        spec_for(n)
        for n in (
            "inject_env_config",
            "prepare_db",
            "deploy_tier",
            "rollback_tier",
            "health_check",
            "smoke_test",
        )
    )
    for name, fn in (
        ("inject_env_config", inject_env_config),
        ("prepare_db", prepare_db),
        ("deploy_tier", deploy_tier),
        ("rollback_tier", rollback_tier),
    ):
        registry.tool(name)(fn)

    @registry.tool("health_check")
    def health(inp: HttpInput, ctx: RunContext) -> HttpOutput:
        return HttpOutput(passed=provider.health_check(ctx).passed)

    @registry.tool("smoke_test")
    def smoke(inp: HttpInput, ctx: RunContext) -> HttpOutput:
        origin = ctx.platform["onprem"]["public_url"]
        _Tier.valid_public_env({"APP_BASE_URL": origin})
        # 외부 경로 HTTP를 확인한다. DB/모든 replica의 준비 검사는 health_check가 담당한다.
        try:
            for path in ("/", "/static/style.css", "/health/ready", "/version"):
                request = Request(origin.rstrip("/") + path, headers={"Cache-Control": "no-cache"})  # noqa: S310 - validated HTTP(S)
                with urlopen(request, timeout=5) as response:  # noqa: S310
                    body = response.read(1024 * 1024)
                    if response.status != 200 or (
                        path == "/version" and json.loads(body).get("release_id") != ctx.run_id
                    ):
                        return HttpOutput(passed=False)
            return HttpOutput(passed=True)
        except (OSError, ValueError):
            return HttpOutput(passed=False)

    return registry


def deployment_plan(run_id: str, project: str, mode: RunMode, *, three: bool) -> Plan:
    def step(sid: str, tool: str, *, tier: str | None = None, **params: Any) -> PlanStep:
        spec = spec_for(tool)
        return PlanStep(
            id=sid,
            tool=tool,
            layer=spec.layer,
            effect=spec.effect,
            target=Target.LOCAL,
            tier=tier,
            params=params,
        )

    steps = [step("deploy.config.local", "inject_env_config", keys=["SECRET_KEY", "DATABASE_URL"])]
    if three:
        steps.append(step("deploy.db.local", "deploy_tier", tier="db"))
    steps += [
        step("deploy.migrate.local", "prepare_db", migrations=["0001"]),
        step("deploy.was.local", "deploy_tier", tier="was"),
    ]
    if three:
        steps.append(step("deploy.web.local", "deploy_tier", tier="web"))
    steps += [step("verify.health.local", "health_check"), step("verify.smoke.local", "smoke_test")]
    return Plan.model_validate(
        {
            "run_id": run_id,
            "project": project,
            "mode": mode,
            "deploy": {"local": {"steps": steps, "signal": "local_verified"}},
        }
    )


async def deploy(directory: Path, source: Path, artifact_file: Path, mode: RunMode) -> dict:
    inventory = json.loads((directory / "inventory.json").read_text())
    _Inventory.model_validate(inventory)
    project = json.loads((directory / "project.json").read_text())["project"]
    artifacts = ReleaseArtifacts.model_validate(json.loads(artifact_file.read_text())["artifacts"])
    tiers = set(inventory["tiers"])
    if tiers not in ({"was"}, {"db", "was", "web"}) or set(artifacts.images) != tiers:
        raise ValueError("인벤토리와 산출물 tier 집합 불일치")
    if artifacts.snapshot != preview(source):
        raise ValueError("빌드 이후 소스 변경; 다시 build 필요")
    rid = "onprem-" + uuid.uuid4().hex
    plan = deployment_plan(rid, project, mode, three="db" in tiers)
    ctx = RunContext(
        rid,
        project=project,
        mode=mode,
        adapter_mode=AdapterMode.REAL,
        platform={"onprem": inventory},
        images={k: v.ref for k, v in artifacts.images.items()},
        release_artifacts=artifacts,
    )
    with closing(DeploymentService(registry_for(OnPremProvider()), directory / "state")) as service:
        service.prepare(plan, ctx, source)
        print(
            json.dumps(
                {"approval": service.approval_view(rid), "images": ctx.images}, ensure_ascii=False
            )
        )
        approved = input("위 이미지로 온프렘 배포를 승인합니까? [y/N] ").strip().lower() == "y"
        service.approve(rid, approver="wsl-cli", approved=approved)
        if not approved:
            return {"status": "DENIED"}
        service.start(rid)
        result = await service.wait(rid)
        return {
            "run_id": rid,
            "status": result.status.value,
            "tracks": {k: v.value for k, v in result.tracks.items()},
            "records": str(directory / "state" / "runs" / rid),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="오프라인 초안·env 생성; 기존 경로 덮어쓰기 금지")
    init.add_argument("--directory", type=Path, required=True)
    init.add_argument("--layout", choices=("was", "three"), required=True)
    init.add_argument("--base-url", required=True)
    init.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), default="linux/amd64")
    builder = sub.add_parser("build", help="사용자 실행: 두 아키텍처 빌드·push 및 digest 기록")
    builder.add_argument("--source", type=Path, default=APP)
    builder.add_argument(
        "--repository", required=True, help="예: DockerHub계정/flaskr (뒤에 -was/-web 추가)"
    )
    builder.add_argument("--output", type=Path, required=True)
    builder.add_argument("--three", action="store_true")
    builder.add_argument("--reuse-web", type=Path)
    for name in ("preflight", "deploy"):
        command = sub.add_parser(name)
        command.add_argument("--directory", type=Path, required=True)
        if name == "deploy":
            command.add_argument("--source", type=Path, default=APP)
            command.add_argument("--artifacts", type=Path, required=True)
            command.add_argument("--mode", choices=("bootstrap", "update"), required=True)
    args = parser.parse_args()
    try:
        if args.command == "init":
            report = initialize(args.directory, args.layout, args.base_url, args.platform)
        elif args.command == "build":
            build(
                args.source,
                args.repository,
                args.output,
                three=args.three,
                reuse_web=args.reuse_web,
            )
            report = {"artifacts": str(args.output)}
        elif args.command == "preflight":
            inventory = json.loads((args.directory / "inventory.json").read_text())
            project = json.loads((args.directory / "project.json").read_text())["project"]
            report = preflight_inventory(inventory, project=project)
        else:
            report = asyncio.run(
                deploy(args.directory, args.source, args.artifacts, RunMode(args.mode))
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return (
            0
            if report.get("passed", True) and report.get("status", "SUCCEEDED") == "SUCCEEDED"
            else 1
        )
    except Exception as error:
        # Pydantic/CLI 원문에는 입력값·URL이 포함될 수 있다.
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(error).__name__,
                    "detail": "설정·이미지·실행 기록 확인 필요. 원문 입력값 출력 생략.",
                },
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
