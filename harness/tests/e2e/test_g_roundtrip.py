"""source=fake: 실제 로컬 Git+계획+승인+실행 장부의 v1→v2→v1 왕복.

Docker/AWS/LLM 실행은 하지 않는다. 외부 실행 툴은 이 테스트의 전용 registry에만 등록한다.
"""

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic import ConfigDict, field_validator

from ddak import app
from ddak.cloud.build import configure_local_build, preflight_local_build
from ddak.cloud.build.codebuild import exported_names
from ddak.cloud.build.image import build_image as local_build
from ddak.core.config import Settings
from ddak.core.contracts.base import ContractModel, ToolInput
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import ToolKind
from ddak.core.contracts.release import ImageArtifact, ReleaseArtifacts
from ddak.core.registry import Registry
from ddak.core.snapshots import digest_bytes, file_manifest, preview
from ddak.executor.engine import RunStatus
from ddak.executor.service import DeploymentService
from ddak.plan.intake import FetchPolicy, WatchTarget, resolve_head
from ddak.plan.intake.watch import write_last_commit
from tests.unit.cloud.build.test_local import FakeRunner
from tests.unit.core.test_app_repository import git
from tests.unit.core.test_candidate import operator_identity  # noqa: F401
from tests.unit.plan.test_flow import FakeJev


class Input(ToolInput):
    model_config = ConfigDict(extra="allow", frozen=True)
    target: str | None = None
    tier: str | None = None


class Output(ContractModel):
    passed: bool = True
    source: str = "fake"
    release_artifacts: ReleaseArtifacts | None = None


@pytest.mark.anyio
@pytest.mark.parametrize("relative_defaults", [False, True])
@pytest.mark.parametrize("local_backend", [False, True])
async def test_watch_manual_approval_git_roundtrip(
    tmp_path, monkeypatch, capsys, relative_defaults, local_backend
):
    started = time.monotonic()
    if relative_defaults:
        monkeypatch.chdir(tmp_path)
    settings = Settings.from_env({})
    if local_backend:
        settings = replace(settings, build_backend="local", image_repository="2026gerbera/flaskr")
    runner = FakeRunner()
    if local_backend:
        configure_local_build(runner=runner)
    state_root = settings.run_dir.parent if relative_defaults else tmp_path / "state"
    real_registry = app.load_tools()
    missing = {name: real_registry.spec(name).owners for name in sorted(real_registry.missing())}
    registry = Registry(real_registry.specs)
    deployed = {}
    calls = []

    def fake_tool(name):
        async def run(inp: Input, ctx: RunContext) -> Output:
            calls.append((name, ctx.run_id))
            assert ctx.candidate_sha  # 빌드·배포는 모두 승인 후보 SHA로 고정
            if name == "build_image":
                snapshot = preview(Path(ctx.build_source))
                digest = snapshot.build_snapshot_hash
                if local_backend:
                    runner.sha = ctx.candidate_sha
                    root = Path(ctx.build_source)
                    runner.payload = {n: (root / n).read_text() for n in file_manifest(root)}
                    index, platforms = exported_names(inp.tier)
                    runner.output = "\n".join(
                        f"{n}={digest_bytes((digest + n).encode())}"
                        for n in [index, *platforms.values()]
                    )
                    result = local_build(inp.tier, ctx)
                    assert result.candidate_sha == ctx.candidate_sha
                    assert result.source.value == "fixture"
                    return Output(release_artifacts=result.release_artifacts)
                image = ImageArtifact(
                    ref="fixture.invalid/flaskr@" + digest,
                    index_digest=digest,
                    platform_digests={
                        p: digest_bytes((digest + p).encode())
                        for p in ("linux/amd64", "linux/arm64")
                    },
                )
                return Output(
                    release_artifacts=ReleaseArtifacts(snapshot=snapshot, images={"was": image})
                )
            if name == "deploy_tier":
                deployed[inp.target] = ctx.images["was"]
            return Output()

        return run

    for spec in registry.specs:
        if spec.kind is ToolKind.TOOL_FN:
            registry.tool(spec.name)(fake_tool(spec.name))

    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(bare))
    work = tmp_path / "checkout"
    git(tmp_path, "clone", str(bare), str(work))
    git(work, "switch", "-c", "prod")
    (work / "was").mkdir()
    (work / "was/app.py").write_text(
        "from flask import Flask\napp = Flask(__name__)\n"
        "@app.get('/')\ndef index():\n    return 'Flask v1'\n"
    )
    (work / "was/Dockerfile").write_text("FROM python:3.13-slim\nCOPY . /app\n")
    (work / "docker").mkdir()
    (work / "docker/was.Dockerfile").write_text("FROM scratch\nCOPY was /app\nCOPY certs/ certs/\n")
    certificate = (
        Path(__file__).parents[1] / "fixtures/certificates/public-ca.pem.txt"
    ).read_bytes()
    (work / "certs").mkdir()
    (work / "certs/global-bundle.pem").write_bytes(certificate * 2)
    (work / "deploy.yaml").write_text(
        "tiers:\n  was:\n    paths: [was, certs]\n    dockerfile: was/Dockerfile\n"
        "env_example: .env.example\n"
    )
    (work / ".env.example").write_text("# No injected settings in this rehearsal\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "Flask base")
    v1 = git(work, "rev-parse", "HEAD")
    git(work, "tag", "-a", "v1", "-m", "Flask base release")
    assert git(work, "rev-parse", "refs/tags/v1") != v1
    git(work, "push", "origin", "prod", "HEAD:main", "HEAD:ai-prod", "refs/tags/v1")
    git(work, "switch", "-c", "ai-prod")
    policy = FetchPolicy(
        allowed_schemes=("file",),
        allowed_hosts=None,
        root=Path("var/sources") if relative_defaults else tmp_path / "intake",
    )
    url = bare.as_uri()
    git(work, "remote", "set-url", "origin", url)
    # 운영 설정은 HTTPS만 허용한다. 이 시험에만 로컬 bare URL을 허용한다.
    from ddak.core.project_settings import ProjectSettings
    from ddak.executor import service as service_module

    class LocalSettings(ProjectSettings):
        @field_validator("repo_url", mode="plain")
        @classmethod
        def repository_url(cls, value):
            assert value == url
            return value

    monkeypatch.setattr(service_module, "ProjectSettings", LocalSettings)
    real_plan = app.plan_deployment
    plan_calls = []

    def plan(request, **kwargs):
        assert request.repo_url == url
        bundle = real_plan(
            request.model_copy(update={"repo_url": bare.as_uri()}),
            **kwargs,
            jev_client=FakeJev(),
        )
        assert bundle.facts.modified_migrations == () and bundle.facts.new_migrations == ()
        plan_calls.append(bundle)
        return bundle

    def resolve(url_arg, ref, *, policy):
        assert url_arg == url
        return resolve_head(bare.as_uri(), ref, policy=policy)

    monkeypatch.setattr(app, "plan_deployment", plan)
    monkeypatch.setattr(app, "resolve_head", resolve)
    monkeypatch.delenv("DDAK_ONPREM_INVENTORY", raising=False)
    connected_repository = app._repository_factory(
        Path("var/checkouts") if relative_defaults else tmp_path / "checkouts", allow_local=True
    )
    scanner_calls = []

    def fixture_scanner(workspace):
        # 이 왕복 시험은 스캐너 설치 여부와 독립적이다. 실제 탐지 능력은 검사하지 않는다.
        scanner_calls.append(workspace)

    def fixture_repository(ctx):
        repository = connected_repository(ctx)
        repository.secret_scan = fixture_scanner
        return repository

    service = DeploymentService(
        registry,
        state_root,
        repository_factory=fixture_repository,
        planning_flow=app._manual_planning(settings, policy),
        build_preflight=lambda ctx: preflight_local_build(ctx.image_repository, runner=runner),
    )
    service.save_project_settings(
        "demo",
        {
            "repo_url": url,
            "watch_branch": "prod",
            "default_targets": "onprem" if local_backend else "both",
            "auto_detect": True,
            "code_patch": False,  # 이 왕복은 코드 패치 없는 배포 경로를 검증한다.
        },
        updated_by="operator",
        expected_version=0,
    )
    rows = []

    async def deploy(rid, source_sha, label, requested_at):
        prepared_at = time.monotonic()
        view = service.approval_view(rid)
        copied_source = service._load_prepared(rid).source
        assert (copied_source / "certs/global-bundle.pem").read_bytes() == certificate * 2
        assert "certs/global-bundle.pem" in file_manifest(copied_source)
        assert service.root.is_absolute() and service.store.path.is_absolute()
        assert service._load_prepared(rid).source.is_absolute()
        assert service.get_run(rid)["status"] == "AWAITING_APPROVAL", service.get_run(rid)["result"]
        assert not any(run == rid for _, run in calls)
        service.approve(rid, approver="operator")
        approved_at = time.monotonic()
        service.start(rid)
        result = await service.wait(rid)
        assert result.status is RunStatus.SUCCEEDED, service.get_run(rid)
        release = service.get_release(rid)
        assert release["source_sha"] == source_sha
        candidate = result.context.candidate_sha
        for ref in (
            ("main", "refs/tags/deployed/onprem")
            if local_backend
            else ("main", "refs/tags/deployed/onprem", "refs/tags/deployed/cloud")
        ):
            assert git(bare, "rev-parse", ref) == candidate
        if local_backend:
            assert "cloud" not in deployed
        else:
            assert deployed["local"] == deployed["cloud"]
        rows.append(
            {
                "phase": label,
                "source_sha": source_sha,
                "candidate_sha": candidate,
                "trigger": view["trigger"],
                "image": deployed["local"],
                "approved_to_record_s": time.monotonic() - approved_at,
                "request_to_record_s": time.monotonic() - requested_at,
                "simulated_approval_s": approved_at - prepared_at,
            }
        )
        return release

    try:
        first_started = time.monotonic()
        first = await service.request_deployment("demo", ref="v1")
        # 수동 checkout 주입 없이 승인 대기 재시작도 조립 factory로 복구한다.
        await service.shutdown()
        service = DeploymentService(
            registry,
            state_root,
            repository_factory=fixture_repository,
            planning_flow=app._manual_planning(settings, policy),
            build_preflight=lambda ctx: preflight_local_build(ctx.image_repository, runner=runner),
        )
        assert service.approval_view(first)["repo_url"] == url
        first_release = await deploy(first, v1, "v1", first_started)
        # 별도 개발 checkout에서 이미지/박스만 추가한다. 앱 checkout/ai-prod는 파이프라인 전용.
        dev = tmp_path / "developer"
        git(tmp_path, "clone", str(bare), str(dev))
        git(dev, "switch", "prod")
        (dev / "was/static").mkdir()
        (dev / "was/static/box.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<rect width="80" height="80" fill="blue"/></svg>\n'
        )
        (dev / "was/app.py").write_text(
            "from flask import Flask\napp = Flask(__name__)\n"
            "@app.get('/')\ndef index():\n"
            "    return '<div><img src=\"/static/box.svg\">Flask v2</div>'\n"
        )
        git(dev, "add", "was")
        git(dev, "commit", "-m", "Add image and box")
        git(dev, "push", "origin", "prod", "HEAD:refs/tags/v2")
        v2 = git(dev, "rev-parse", "HEAD")
        target = WatchTarget("demo", url, "prod", "local" if local_backend else "both")
        write_last_commit(policy, target, v1)
        monkeypatch.setenv("DDAK_WATCH_REPO_URL", url)
        monkeypatch.setenv("DDAK_SOURCES_DIR", str(policy.root))
        monkeypatch.setattr(app.FetchPolicy, "from_env", classmethod(lambda cls: policy))
        assert app._watch_targets(service) == [target]
        real_watcher = app.Watcher

        def watcher(targets, handler, *, policy, **kwargs):
            return real_watcher(
                targets,
                handler,
                policy=policy,
                interval_s=0.01,
                resolve=lambda url, ref, policy: resolve(url, ref, policy=policy),
                warm=lambda *a: v1,
            )

        @asynccontextmanager
        async def lifespan(a):
            a.state.deployment = service
            yield

        monkeypatch.setattr(app, "Watcher", watcher)
        application = FastAPI(lifespan=lifespan)
        app._attach_watch(application, settings)
        second_started = time.monotonic()
        async with application.router.lifespan_context(application):
            async with asyncio.timeout(5):
                while True:
                    pending = [r for r in service.list_runs() if r["run_id"] != first]
                    if pending:
                        break
                    await asyncio.sleep(0.01)
            second = pending[0]["run_id"]
            second_release = await deploy(second, v2, "v2-image-box", second_started)
        back_started = time.monotonic()
        back = await service.request_deployment("demo", ref="v1")
        back_release = await deploy(back, v1, "v1-redeploy", back_started)
        assert first_release["source_files"] == back_release["source_files"]
        assert first_release["source_files"] != second_release["source_files"]
        assert rows[0]["image"] == rows[2]["image"] != rows[1]["image"]
        assert [r["trigger"] for r in rows] == ["manual", "auto", "manual"]
        assert len(plan_calls) == 3
        assert len(scanner_calls) >= 3
        assert all(not p.context.toggles["code_patch"] for p in plan_calls)
        assert not any("migration" in name for name in file_manifest(dev))
        internal = {
            "acquire_deploy_lock": "Store.acquire",
            "record_deploy_log": "Store.finish",
            "preflight_check": "O1 운영 스크립트",
            "reset_demo_state": "O1 운영 스크립트",
        }
        requested_tools = {
            step.tool
            for bundle in plan_calls
            for section in (
                bundle.plan.build,
                bundle.plan.deploy.local,
                bundle.plan.deploy.cloud,
                bundle.plan.verify,
            )
            for step in section.steps
        }
        report = {
            "source": "fake",
            "build_backend": settings.build_backend,
            "secret_scanner": {
                "source": "fixture",
                "result": "passed",
                "calls": len(scanner_calls),
            },
            "roundtrip": rows,
            "total_s": time.monotonic() - started,
            "registry_missing": missing,
            "implemented_outside_registry": internal,
            "missing_real_tools": {k: v for k, v in missing.items() if k not in internal},
            "required_pipeline_missing": sorted(
                requested_tools & (missing.keys() - internal.keys())
            ),
            "operational_gaps": [
                "watch.py 단독 기본 main→prod 정정 요청(O2); app 조립은 저장 설정 반영",
                "실제 Docker/AWS/LLM 및 사람 승인 소요 시간 미검증",
            ],
        }
        with capsys.disabled():
            sys.stdout.write("G_E2E_REPORT " + json.dumps(report, ensure_ascii=False) + "\n")
    finally:
        await service.shutdown()
        configure_local_build()
