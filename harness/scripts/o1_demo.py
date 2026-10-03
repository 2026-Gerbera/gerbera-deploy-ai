#!/usr/bin/env python3
"""O1 fixture 리허설. real/local 요청을 fixture로 대체하지 않는다."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ddak.core.config import AdapterMode
from ddak.core.contracts.base import ContractModel, ToolInput
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import By, RunMode, Source, Target
from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.plan import DeploySections, Plan, Planner, PlanStep, Section
from ddak.core.contracts.release import ImageArtifact, ImageObservation, ReleaseArtifacts
from ddak.core.registry import CATALOG, Registry
from ddak.core.snapshots import digest_bytes, digest_json
from ddak.executor.service import DeploymentService
from ddak.onprem.deploy import preflight_inventory, reset_demo

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "o1_demo"
STATE = ROOT / "var" / "o1-demo"
PROJECT = "ddak-o1-demo"
OWNER = {"project": PROJECT, "source": "fixture", "schema": "o1-demo/v1"}
BANNER = "fixture: cloud/build/AI simulation, no real cloud"
EXPECTED = {
    "success": ("SUCCEEDED", "v2", "v2"),
    "local_fail": ("FAILED_LOCAL", "v1", "v2"),
    "cloud_fail": ("FAILED_CLOUD", "v2", "v1"),
    "parity_fail": ("PARITY_FAILED", "v2", "v1"),
}
TOOLS = (
    "build_image",
    "prepare_db",
    "deploy_tier",
    "rollback_tier",
    "health_check",
    "smoke_test",
    "ensure_tls",
    "verify_tls",
    "compare_env_results",
    "post_report",
)


class FixtureInput(ToolInput):
    target: Target | None = None
    tier: str | None = None
    lock_token: str | None = None
    migrations: list[str] = Field(default_factory=list)


class FixtureOutput(ContractModel):
    source: Literal["fixture"] = "fixture"
    source_mode: Literal["fixture"] = "fixture"
    provider: Literal["fixture"] = "fixture"
    function: str
    passed: bool = True
    changed: bool = False
    release_artifacts: ReleaseArtifacts | None = None
    observation: ImageObservation | None = None
    migration: dict[str, Any] | None = None


class FixtureRuntime:
    """독립 레지스트리의 결정적 대역. canonical 전역 등록은 건드리지 않는다."""

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.registry = Registry(CATALOG, package=None)
        self.views: dict[str, dict[str, Any]] = {}
        self.current: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []
        for name in TOOLS:
            self.registry.tool(name)(self._function(name))

    def _function(self, name: str) -> Callable[..., Any]:
        async def invoke(inp: FixtureInput, ctx: RunContext) -> FixtureOutput:
            if ctx.adapter_mode is not AdapterMode.FAKE:
                raise RuntimeError("fixture 함수는 fake 컨텍스트에서만 실행한다")
            target = inp.target.value if inp.target else None
            self.calls.append({"tool": name, "target": target, "run_id": ctx.run_id})
            output: dict[str, Any] = {"function": name}
            updating = ctx.mode is RunMode.UPDATE
            if name == "build_image":
                snapshot = self.views[ctx.run_id]["snapshot"]
                # 해시는 승인 화면의 실제 소스/빌드 해시에서 유도하지만 이미지는 가상이다.
                digest = digest_json({"fixture_image": snapshot["build_snapshot_hash"]})
                output["release_artifacts"] = ReleaseArtifacts(
                    snapshot=snapshot,
                    images={
                        "was": ImageArtifact(
                            ref=f"fixture.invalid/ddak/was@{digest}",
                            index_digest=digest,
                            platform_digests={
                                platform: digest_json({"index": digest, "platform": platform})
                                for platform in ("linux/amd64", "linux/arm64")
                            },
                        )
                    },
                )
            elif name == "prepare_db":
                output["migration"] = {
                    "event": "MIGRATE_RESULT",
                    "status": "ok",
                    "phases": [
                        {
                            "phase": phase,
                            "status": "ok",
                            "changed": phase == "up",
                            "migrations": inp.migrations,
                        }
                        for phase in ("precheck", "up", "verify")
                    ],
                }
            elif name == "deploy_tier":
                if target is None or ctx.release_artifacts is None:
                    raise RuntimeError("빌드 산출물/대상이 없다")
                self.current[target] = ctx.run_id
                artifact = ctx.release_artifacts.images["was"]
                platform = "linux/arm64" if target == "local" else "linux/amd64"
                output.update(
                    changed=True,
                    observation=ImageObservation(
                        platform=platform,
                        platform_digest=artifact.platform_digests[platform],
                    ),
                )
            elif name == "rollback_tier":
                if target is None:
                    raise RuntimeError("롤백 대상이 없다")
                previous = ctx.previous_release.get(target)
                if previous:
                    self.current[target] = previous["release_id"]
                else:
                    self.current.pop(target, None)
                output["changed"] = True
            elif updating and name == "smoke_test":
                output["passed"] = self.scenario != f"{target}_fail"
            elif updating and name == "compare_env_results":
                output["passed"] = self.scenario != "parity_fail"
            return FixtureOutput(**output)

        return invoke


def fixture_plan(run_id: str, *, bootstrap: bool) -> Plan:
    catalog = Registry(CATALOG)

    def step(identifier: str, tool: str, **kwargs: Any) -> PlanStep:
        spec = catalog.spec(tool)
        return PlanStep(id=identifier, tool=tool, layer=spec.layer, effect=spec.effect, **kwargs)

    def track(target: Target) -> Section:
        suffix = target.value
        gates = ["images_ready"]
        steps = [
            step(
                f"deploy.db.{suffix}",
                "prepare_db",
                target=target,
                wait_for=gates,
                params={"migrations": ["001_users"] if not bootstrap else []},
            ),
            step(f"deploy.was.{suffix}", "deploy_tier", target=target, tier="was"),
            step(f"verify.health.{suffix}", "health_check", target=target),
            step(f"verify.smoke.{suffix}", "smoke_test", target=target),
        ]
        if target is Target.CLOUD:
            steps.insert(1, step("deploy.tls.cloud", "ensure_tls", target=target))
            steps.append(step("verify.tls.cloud", "verify_tls", target=target))
        return Section(steps=steps, signal=f"{suffix}_verified")

    return Plan(
        run_id=run_id,
        project=PROJECT,
        mode=RunMode.BOOTSTRAP if bootstrap else RunMode.UPDATE,
        planner=Planner(by=By.RULE, provider="o1-fixture", source=Source.FIXTURE),
        toggles={"code_patch": not bootstrap},
        build=Section(steps=[step("build.was", "build_image", tier="was")], signal="images_ready"),
        deploy=DeploySections(local=track(Target.LOCAL), cloud=track(Target.CLOUD)),
        verify=Section(
            steps=[
                step(
                    "verify.compare",
                    "compare_env_results",
                    wait_for=["local_verified", "cloud_verified"],
                ),
                step("verify.report", "post_report", run="finally"),
            ]
        ),
    )


def _check_path(root: Path) -> None:
    if root.name != "o1-demo" or any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("o1-demo 전용 디렉토리만 지원하며 심볼릭 링크는 거부한다")


def initialize(root: Path) -> None:
    _check_path(root)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "owner.json"
    if marker.exists():
        if marker.is_symlink() or json.loads(marker.read_text()) != OWNER:
            raise ValueError("fixture 소유 표식이 다르다")
    elif any(root.iterdir()):
        raise ValueError("기존 디렉토리는 소유가 확인되지 않아 사용할 수 없다")
    else:
        marker.write_text(json.dumps(OWNER) + "\n")
    if any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError("fixture 상태의 심볼릭 링크는 지원하지 않는다")


async def run_fixture(
    root: Path,
    scenario: str,
    *,
    yes: bool = False,
    ask: Callable[[str], str] = input,
    emit: Callable[[str], None] = print,
) -> dict[str, Any]:
    if scenario not in EXPECTED:
        raise ValueError("알 수 없는 fixture 시나리오")
    emit(BANNER)
    initialize(root)
    runtime = FixtureRuntime(scenario)
    service = DeploymentService(runtime.registry, root)
    started = time.monotonic()
    try:
        with service.store.connection() as db:
            if db.execute("SELECT 1 FROM runs LIMIT 1").fetchone():
                raise ValueError("기존 실행 기록이 있다. make demo-reset 후 다시 실행한다")
        prefix = f"o1-{uuid.uuid4().hex}"
        runs = {version: f"{prefix}-{version}" for version in ("v1", "v2")}
        for version, run_id in runs.items():
            plan = fixture_plan(run_id, bootstrap=version == "v1")
            context = RunContext(
                run_id=run_id,
                project=PROJECT,
                mode=plan.mode,
                adapter_mode=AdapterMode.FAKE,
                toggles=plan.toggles,
            )
            patch = (FIXTURES / "v2.patch").read_bytes() if version == "v2" else None
            # fixture 전용 검사 대역이며 실제 앱의 패치 검사 결과로 사용하지 않는다.
            patch_meta = (
                {
                    "passed": True,
                    "patch_sha256": digest_bytes(patch),
                    "reason": "fixture v2 패치 검사 대역",
                    "reuse": False,
                    "source": "fixture",
                }
                if patch is not None
                else None
            )
            service.prepare(
                plan,
                context,
                FIXTURES / "source",
                patch=patch,
                patch_meta=patch_meta,
            )
            view = service.approval_view(run_id)
            runtime.views[run_id] = view
            emit(
                json.dumps(
                    {
                        "approval": version,
                        "source": "fixture",
                        "run_id": run_id,
                        "subjects": view["subjects"],
                        "snapshot": view["snapshot"],
                        "plan": {
                            section: [step.tool for step in steps]
                            for section, steps in (
                                ("build", plan.build.steps),
                                ("local", plan.deploy.local.steps),
                                ("cloud", plan.deploy.cloud.steps),
                                ("verify", plan.verify.steps),
                            )
                        },
                    },
                    ensure_ascii=False,
                )
            )
            if view["patch"]:
                emit(view["patch"])
        # 한 화면의 한 번의 응답. 승인 기록은 v1/v2와 deploy/patch별로 분리한다.
        approved = (
            yes
            or ask("fixture v1 초기 배포와 v2 패치 배포를 승인합니까? [y/n] ").strip().lower()
            == "y"
        )
        for run_id in runs.values():
            service.approve(run_id, approver="fixture-cli", approved=approved)
        if not approved:
            summary = {"source": "fixture", "source_mode": "fixture", "status": "DENIED"}
        else:
            timings: dict[str, float] = {}
            results: dict[str, Any] = {}
            for version, run_id in runs.items():
                begin = time.monotonic()
                service.start(run_id)
                result = await service.wait(run_id)
                results[version] = result
                timings[version] = round(time.monotonic() - begin, 4)
                emit(f"fixture {version}: run_id={run_id} status={result.status.value}")
                if version == "v1" and result.status.value != "SUCCEEDED":
                    raise RuntimeError(f"fixture v1 초기 배포 실패: {result.status.value}")
            environments = service.store.environments(PROJECT)
            versions = {rid: version for version, rid in runs.items()}
            state = {
                target: versions.get((row["current"] or {}).get("release_id"), "unknown")
                for target, row in environments.items()
            }
            observed = {
                target: versions.get(rid, "unknown") for target, rid in runtime.current.items()
            }
            result = results["v2"]
            expected_status, local, cloud = EXPECTED[scenario]
            expected_state = {"local": local, "cloud": cloud}
            manifest = json.loads((root / "runs" / runs["v2"] / "release.json").read_text())
            summary = {
                "source": "fixture",
                "source_mode": "fixture",
                "adapter_mode": "fake",
                "scenario": scenario,
                "status": result.status.value,
                "expected_status": expected_status,
                "expectation_met": result.status.value == expected_status
                and state == expected_state
                and observed == expected_state,
                "runs": runs,
                "state": state,
                "fixture_observed": observed,
                "environment_status": {key: row["status"] for key, row in environments.items()},
                "source_files": manifest["source_files"],
                "snapshot": manifest["source"],
                "timing_s": timings,
                "elapsed_s": round(time.monotonic() - started, 4),
                "gates": result.gates,
                "calls": runtime.calls,
                "records": [
                    {"step": r.step_id, "status": r.status, "error": r.error, "output": r.output}
                    for r in result.records
                ],
                "real_cloud_verified": False,
                "live_three_minute_verified": False,
            }
        (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        emit(
            json.dumps(
                {key: value for key, value in summary.items() if key not in {"calls", "records"}},
                ensure_ascii=False,
            )
        )
        return summary
    finally:
        service.close()


def _docker(args: Sequence[str], *, timeout: float = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def docker_preflight() -> dict[str, Any]:
    """Docker CLI만 제한 시간으로 조회한다. 자격증명 파일은 읽지 않는다."""
    try:
        endpoint = os.environ.get("DOCKER_HOST")
        if not endpoint:
            context = _docker(["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"])
            if context.returncode:
                return {"status": "unavailable"}
            endpoint = context.stdout.strip()
        if not endpoint.startswith("unix://"):
            return {"status": "unsupported_remote", "detail": "로컬 Unix socket만 지원"}
        result = _docker(["--host", endpoint, "info", "--format", "{{.ServerVersion}}"])
        return {
            "status": "ready" if result.returncode == 0 else "unavailable",
            "endpoint": endpoint,
        }
    except FileNotFoundError:
        return {"status": "missing"}
    except subprocess.TimeoutExpired:
        return {"status": "timeout"}


def preflight() -> dict[str, Any]:
    from ddak.app import load_tools

    registry = load_tools()
    registry_missing = sorted(registry.missing())
    implemented_internal = {
        "acquire_deploy_lock": "Store.acquire + service",
        "record_deploy_log": "Store.finish",
        "preflight_check": "local CLI only",
        "reset_demo_state": "fixture only",
    }
    missing = sorted(set(registry_missing) - implemented_internal.keys())
    return {
        "source": "local-inspection",
        "docker": docker_preflight(),
        "registered": sorted(registry.registered()),
        "registry_missing": registry_missing,
        "implemented_internal": implemented_internal,
        "missing": missing,
        "missing_owners": {name: registry.spec(name).owners for name in missing},
        "missing_scope": (
            "missing은 registry_missing에서 implemented_internal 항목을 제외한 목록이다. "
            "preflight_check는 로컬 CLI, reset_demo_state는 fixture 전용이며 "
            "클라우드 운영 기능의 준비 완료를 뜻하지 않는다."
        ),
        "real_pipeline": "dry-run only; 팀 빌드/헬스/스모크/클라우드 연결 검증 필요",
        "credentials_checked": False,
        "cloud_checked": False,
        "ai_checked": False,
    }


@contextmanager
def reset_guard(root: Path) -> Iterator[None]:
    _check_path(root)
    if not root.exists():
        raise ValueError("fixture 상태 디렉토리가 없다")
    marker = root / "owner.json"
    if marker.is_symlink() or not marker.is_file() or json.loads(marker.read_text()) != OWNER:
        raise ValueError("소유가 확인되지 않은 디렉토리는 초기화하지 않는다")
    allowed = {
        "owner.json",
        "controller.lock",
        "ddak.sqlite",
        "ddak.sqlite-wal",
        "ddak.sqlite-shm",
        "runs",
        "summary.json",
    }
    if any(p.name not in allowed for p in root.iterdir()) or any(
        p.is_symlink() for p in root.rglob("*")
    ):
        raise ValueError("알 수 없는 파일 또는 링크가 있어 초기화를 거부한다")
    with (root / "controller.lock").open("a") as lease:
        try:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("활성 컨트롤러가 있어 초기화할 수 없다") from None
        db_path = root / "ddak.sqlite"
        if db_path.exists():
            with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as db:
                busy = db.execute("SELECT 1 FROM locks LIMIT 1").fetchone()
                unsafe = db.execute(
                    "SELECT 1 FROM runs WHERE status IN ('RUNNING','NEEDS_HUMAN') "
                    "OR project != ? LIMIT 1",
                    (PROJECT,),
                ).fetchone()
                human = db.execute(
                    "SELECT 1 FROM env_release WHERE status='NEEDS_HUMAN' LIMIT 1"
                ).fetchone()
                if busy or unsafe or human:
                    raise ValueError("RUNNING/NEEDS_HUMAN/미해제 잠금은 수동 확인이 필요하다")
                rows = db.execute("SELECT manifest FROM releases").fetchall()
                if any(json.loads(row[0]).get("source_mode") != "fake" for row in rows):
                    raise ValueError("fixture가 아닌 릴리스는 초기화하지 않는다")
        yield


def _reset_containers() -> int:
    probe = docker_preflight()
    if probe["status"] != "ready":
        raise ValueError(f"Docker 초기화 불가: {probe['status']}")
    prefix = ["--host", probe["endpoint"]]
    listed = _docker(
        [
            *prefix,
            "ps",
            "-aq",
            "--filter",
            "label=ddak.demo=true",
            "--filter",
            f"label=ddak.project={PROJECT}",
        ]
    )
    if listed.returncode:
        raise ValueError("소유 컨테이너 목록 조회 실패")
    ids = listed.stdout.split()
    if any(not re.fullmatch(r"[0-9a-f]{12,64}", cid) for cid in ids):
        raise ValueError("Docker ID가 잘못됐다")
    for cid in ids:
        inspected = _docker([*prefix, "inspect", "--format", "{{json .Config.Labels}}", cid])
        labels = json.loads(inspected.stdout) if inspected.returncode == 0 else {}
        if not isinstance(labels, dict) or (
            labels.get("ddak.demo"),
            labels.get("ddak.project"),
        ) != ("true", PROJECT):
            raise ValueError("데모/프로젝트 소유 라벨을 재확인할 수 없다")
    for cid in ids:
        if _docker([*prefix, "rm", "-f", cid], timeout=10).returncode:
            raise ValueError("소유 컨테이너 삭제 실패; 장부 유지")
    return len(ids)


def reset_fixture(root: Path, *, containers: bool = False) -> dict[str, Any]:
    with reset_guard(root):
        removed = _reset_containers() if containers else 0
        for path in root.iterdir():
            # 컨트롤러 잠금 inode를 유지한다. 다른 컨트롤러와 새 잠금 파일 경합을 막는다.
            if path.name in {"owner.json", "controller.lock"}:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    return {
        "source": "fixture",
        "status": "RESET",
        "containers_removed": removed,
        "containers_checked": containers,
        "cloud_touched": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "local", "real"), default="fixture")
    parser.add_argument("--scenario", choices=tuple(EXPECTED), default="success")
    parser.add_argument("--yes", action="store_true", help="fixture 테스트만 자동 승인")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument(
        "--containers", action="store_true", help="reset 시 이중 라벨 컨테이너도 정리"
    )
    parser.add_argument("--inventory", type=Path, help="provider 모양 onprem 인벤토리 JSON")
    parser.add_argument("--project", default="flaskr")
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(os.environ.get("DDAK_RUN_DIR", str(ROOT / "var" / "runs"))).parent,
    )
    parser.add_argument("--release", help="복구할 REAL 성공 run_id")
    args = parser.parse_args(argv)
    if args.yes and args.mode != "fixture":
        parser.error("--yes는 fixture 테스트에만 사용할 수 있다")
    if args.containers and not args.reset:
        parser.error("--containers는 --reset과 함께 사용한다")
    try:
        if args.preflight and args.inventory:
            inventory = json.loads(args.inventory.read_text())
            report = preflight_inventory(inventory, project=args.project)
            print(json.dumps(report, ensure_ascii=False))
            return 0 if report["passed"] else 3
        if args.reset and args.mode == "real":
            if not args.inventory or not args.release:
                parser.error("real reset에는 --inventory와 --release가 필요하다")
            inventory = json.loads(args.inventory.read_text())
            report = reset_demo(args.state, args.project, inventory, args.release)
            print(json.dumps(report, ensure_ascii=False))
            return 0
        if args.reset:
            if args.mode != "fixture":
                raise ValueError("실배포 reset은 지원하지 않는다; provider 소유 정리 절차 필요")
            print(json.dumps(reset_fixture(STATE, containers=args.containers), ensure_ascii=False))
            return 0
        if args.preflight or args.mode != "fixture":
            report = preflight()
            print(json.dumps(report, ensure_ascii=False))
            if args.preflight:
                return 0 if report["docker"]["status"] == "ready" and not report["missing"] else 3
            if args.mode == "local":
                directory = ROOT / "tests" / "docker"
                if report["docker"]["status"] == "ready" and list(directory.glob("test_*.py")):
                    print("source=real-docker: provider 통합 테스트; 전체 서비스 배포 증명은 아님")
                    return subprocess.run(
                        [sys.executable, "-m", "pytest", "-m", "docker", str(directory)],
                        cwd=ROOT,
                        env={**os.environ, "DDAK_TEST_DOCKER": "1"},
                        check=False,
                    ).returncode
            print("NEEDS_CONTEXT: dry-run only; 실이미지 산출물과 HTTP health/smoke 연결 필요")
            return 3
        summary = asyncio.run(run_fixture(STATE, args.scenario, yes=args.yes))
        return 0 if summary.get("expectation_met") else 1
    except (
        DdakToolError,
        ValueError,
        RuntimeError,
        OSError,
        EOFError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as exc:
        print(f"BLOCKED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
