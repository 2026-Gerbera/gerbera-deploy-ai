"""source=fixture: 실제 로컬 Git·계획·패치·승인·릴리스 결합의 수정11 E2E.

LLM provider, 비밀검사, 빌드, 배포, HTTP는 fixture다. 외부 서비스는 실행하지 않는다.
원본 생성기와 결정적 의도 렌더러, 승인 관문, Git 후보, 성공 원장은 실제 코드다.
"""

import ast
import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from pydantic import ConfigDict, field_validator

from ddak import app
from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.config import Settings
from ddak.core.contracts.base import ContractModel, ToolInput
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source, Target, ToolKind
from ddak.core.contracts.release import ImageArtifact, ReleaseArtifacts
from ddak.core.contracts.tools.smoke_test import SmokeTestInput
from ddak.core.patch_ledger import reuse_patches
from ddak.core.project_settings import ProjectSettings
from ddak.core.registry import Registry
from ddak.core.snapshots import digest_bytes, file_manifest, preview
from ddak.executor.engine import RunStatus
from ddak.executor.service import DeploymentService
from ddak.plan.intake import FetchPolicy, WatchTarget
from ddak.plan.patch import pipeline
from ddak.verify.smoke.fake import FakeSmokeAdapter
from ddak.verify.smoke.logic import run_smoke
from tests.unit.core.test_app_repository import git
from tests.unit.core.test_candidate import operator_identity  # noqa: F401
from tests.unit.plan.test_flow import FakeJev

APP_FILE = "was/app.py"
ORIGINAL = "import os\nSECRET_KEY = '" + "dev'\nVERSION = 1\n"
UPDATED = "import os\nVERSION = 2\nSECRET_KEY = '" + "dev'\n"
ENV_SENTINEL = "fixture-" + "example-value"


class FixtureProvider:
    name = "fixture"

    def __init__(self):
        self.requests = []
        self.targets = {APP_FILE: "secret_key"}

    def complete(self, request: AIRequest) -> AIResponse:
        assert request.purpose == "patch_config"
        assert request.prompt_version == "patch_config-intents-v1"
        data = json.loads(
            request.user.split("<untrusted_data>", 1)[1].split("</untrusted_data>")[0]
        )
        assert set(data) == {"targets", "allowed_keys"}
        assert data["allowed_keys"] == sorted(set(data["allowed_keys"]))
        assert {"SECRET_KEY", "APP_BASE_URL", "FIXTURE_SECRET_KEY"} <= set(data["allowed_keys"])
        assert "DATABASE_URL_MIGRATOR" not in data["allowed_keys"]
        assert ENV_SENTINEL not in request.user
        assert all(t["key"] in data["allowed_keys"] for t in data["targets"])
        assert all(set(t) == {"file", "line", "pattern_id", "key"} for t in data["targets"])
        assert all(
            t["file"] in self.targets and t["pattern_id"] == self.targets[t["file"]]
            for t in data["targets"]
        )
        assert "dev" not in request.user and "VERSION" not in request.user
        self.requests.append(data)
        return AIResponse(
            text=json.dumps({"intents": data["targets"], "reason": "서명 키를 환경변수로 전환"}),
            source=Source.REPLAY,
        )


class FixtureInput(ToolInput):
    model_config = ConfigDict(extra="allow", frozen=True)
    target: str | None = None
    tier: str | None = None


class FixtureOutput(ContractModel):
    passed: bool = True
    source: str = "fixture"
    release_artifacts: ReleaseArtifacts | None = None


@dataclass
class Rig:
    service: DeploymentService
    settings: Settings
    policy: FetchPolicy
    target: WatchTarget
    bare: Path
    developer: Path
    source_sha: str
    provider: FixtureProvider
    bundles: list
    calls: list
    scans: list
    binding_mode: str = "approved"

    async def prepare(self):
        return await app._prepare_commit(
            self.service, self.settings, self.target, self.source_sha, policy=self.policy
        )

    def toggle(self, enabled):
        saved = self.service.get_project_settings("demo")
        self.service.save_project_settings(
            "demo",
            {"code_patch": enabled},
            updated_by="fixture-operator",
            expected_version=saved["version"],
        )

    def update_prod(self):
        (self.developer / APP_FILE).write_text(UPDATED)
        git(self.developer, "add", APP_FILE)
        git(self.developer, "commit", "-m", "Developer source changes")
        git(self.developer, "push", "origin", "prod")
        self.source_sha = git(self.developer, "rev-parse", "HEAD")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(bare))
    developer = tmp_path / "developer"
    git(tmp_path, "clone", str(bare), str(developer))
    git(developer, "switch", "-c", "prod")
    (developer / "was").mkdir()
    (developer / APP_FILE).write_text(ORIGINAL)
    (developer / "was/Dockerfile").write_text("FROM scratch\nCOPY . /app\n")
    (developer / "deploy.yaml").write_text(
        "tiers:\n  was:\n    paths: [was]\n    dockerfile: was/Dockerfile\n"
        "env_example: .env.example\n"
    )
    (developer / ".env.example").write_text(
        "# source=fixture; no credentials\n"
        f"FIXTURE_SECRET_KEY={ENV_SENTINEL}\nDATABASE_URL_MIGRATOR={ENV_SENTINEL}\n"
    )
    git(developer, "add", ".")
    git(developer, "commit", "-m", "Developer configuration fixture")
    source_sha = git(developer, "rev-parse", "HEAD")
    git(developer, "push", "origin", "prod", "HEAD:main", "HEAD:ai-prod")
    url = bare.as_uri()

    class LocalSettings(ProjectSettings):
        @field_validator("repo_url", mode="plain")
        @classmethod
        def local_repository(cls, value):
            assert value == url
            return value

    monkeypatch.setattr("ddak.executor.service.ProjectSettings", LocalSettings)
    monkeypatch.delenv("DDAK_ONPREM_INVENTORY", raising=False)
    settings = Settings(run_dir=tmp_path / "state/runs", ai_retries=0)
    policy = FetchPolicy(allowed_schemes=("file",), allowed_hosts=None, root=tmp_path / "intake")
    provider = FixtureProvider()
    real_proposer = app.propose_intents

    def propose(root, targets, ctx, *, settings):
        return real_proposer(root, targets, ctx, settings=settings, provider=provider)

    monkeypatch.setattr(app, "propose_intents", propose)
    scans = []

    def scan_patch(root, patch):
        assert patch and (root / APP_FILE).exists()
        scans.append({"source": "fixture", "kind": "patch"})

    # scanner 인자로 전달하므로 승인 메타에도 gitleaks=fixture가 남는다.
    real_prepare_patch = app.prepare_patch

    def prepare_patch(*args, **kwargs):
        return real_prepare_patch(*args, **kwargs, scanner=scan_patch)

    monkeypatch.setattr(app, "prepare_patch", prepare_patch)
    monkeypatch.setattr(pipeline, "strict_patch_scan", scan_patch)
    bundles = []
    real_plan = app.plan_deployment

    def plan(request, **kwargs):
        kwargs["jev_client"] = FakeJev()
        bundle = real_plan(request, **kwargs)
        bundles.append(bundle)
        return bundle

    monkeypatch.setattr(app, "plan_deployment", plan)
    repository_factory = app._repository_factory(tmp_path / "checkouts", allow_local=True)

    def repository(ctx):
        connected = repository_factory(ctx)

        def scan_source(root):
            assert root.exists()
            scans.append({"source": "fixture", "kind": "source-or-candidate"})

        connected.secret_scan = scan_source
        return connected

    registry = Registry(app.load_tools().specs)
    calls = []
    service = DeploymentService(registry, tmp_path / "state", repository_factory=repository)
    result = Rig(
        service,
        settings,
        policy,
        WatchTarget("demo", url, "prod", "local"),
        bare,
        developer,
        source_sha,
        provider,
        bundles,
        calls,
        scans,
    )

    def fixture_tool(name):
        async def run(inp: FixtureInput, ctx: RunContext) -> ContractModel:
            calls.append((name, ctx))
            assert ctx.candidate_sha
            if name == "build_image":
                root = Path(ctx.build_source)
                approved = ctx.source_binding
                assert approved.patch_sha256
                assert approved.source_snapshot_hash != approved.build_snapshot_hash
                assert preview(root).build_snapshot_hash == approved.build_snapshot_hash
                assert (root / APP_FILE).read_text() != (developer / APP_FILE).read_text()
                tree = ast.parse((root / APP_FILE).read_text())
                key_read = next(
                    n.value
                    for n in tree.body
                    if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "SECRET_KEY" for t in n.targets)
                )
                assert ast.unparse(key_read) == "os.environ['SECRET_KEY']"
                assert git(bare, "rev-parse", "ai-prod") == ctx.candidate_sha
                assert (
                    git(bare, "show", f"{ctx.candidate_sha}:{APP_FILE}")
                    == (root / APP_FILE).read_text().strip()
                )
                binding = approved
                if result.binding_mode == "original":
                    binding = preview(developer)
                elif result.binding_mode == "patched":
                    binding = preview(root)
                digest = approved.build_snapshot_hash
                image = ImageArtifact(
                    ref="fixture.invalid/app@" + digest,
                    index_digest=digest,
                    platform_digests={
                        p: digest_bytes((digest + p).encode())
                        for p in ("linux/amd64", "linux/arm64")
                    },
                )
                return FixtureOutput(
                    release_artifacts=ReleaseArtifacts(
                        snapshot=binding,
                        images={"was": image},
                    )
                )
            if name == "smoke_test":
                assert inp.target == "local"
                return run_smoke(
                    FakeSmokeAdapter(Target.LOCAL),
                    SmokeTestInput(run_id=ctx.run_id, target="local"),
                    ctx,
                )
            return FixtureOutput()

        return run

    for spec in registry.specs:
        if spec.kind is ToolKind.TOOL_FN:
            registry.tool(spec.name)(fixture_tool(spec.name))
    service.save_project_settings(
        "demo",
        {"repo_url": url, "watch_branch": "prod", "default_targets": "onprem", "code_patch": True},
        updated_by="fixture-operator",
        expected_version=0,
    )
    return result


async def deploy(rig):
    service = rig.service
    rid = await rig.prepare()
    assert service.get_run(rid)["status"] == "AWAITING_APPROVAL", service.get_run(rid)["result"]
    view = service.approval_view(rid)
    prepared = service._load_prepared(rid)
    snapshot = prepared.snapshot
    assert view["missing_env_keys"] == []
    assert view["patch"] is None
    assert view["patch_meta"] is not None, view["preparation_warnings"]
    assert view["patch_meta"]["passed"] is True
    assert view["patch_meta"]["gitleaks"] == "fixture"
    assert set(view["patch_meta"]["patterns"]) == set(rig.provider.targets.values())
    assert view["patch_meta"]["target_hashes"] == {
        name: prepared.source_files[name]["sha256"] for name in rig.provider.targets
    }
    assert view["subjects"]["patch"] == digest_bytes(prepared.patch)
    assert snapshot.patch_sha256 == view["subjects"]["patch"]
    assert not any(ctx.run_id == rid for _, ctx in rig.calls)
    assert (prepared.source / APP_FILE).read_text() == (rig.developer / APP_FILE).read_text()
    records = service.approve(rid, approver="fixture-operator")
    assert any(record.kind == "patch" and record.snapshot == snapshot for record in records)
    service.start(rid)
    result = await service.wait(rid)
    assert result.status is RunStatus.SUCCEEDED, [(r.step_id, r.error) for r in result.records]
    release = service.get_release(rid)
    assert release["source_sha"] == rig.source_sha
    assert release["source"] == snapshot.model_dump(mode="json")
    assert release["artifacts"]["snapshot"] == release["source"]
    build = next(ctx for name, ctx in rig.calls if name == "build_image" and ctx.run_id == rid)
    assert release["files"] == file_manifest(Path(build.build_source))
    assert release["source_files"] == file_manifest(rig.developer)
    entry = release["patch_ledger"][APP_FILE]
    assert entry["result_sha256"] == digest_bytes(
        (Path(build.build_source) / APP_FILE).read_bytes()
    )
    assert release["images"]["was"] == release["artifacts"]["images"]["was"]["ref"]
    for ref in ("main", "refs/tags/deployed/onprem", "ai-prod"):
        assert git(rig.bare, "rev-parse", ref) == result.context.candidate_sha
    assert git(rig.bare, "rev-parse", "prod") == rig.source_sha
    names = [name for name, ctx in rig.calls if ctx.run_id == rid]
    assert all(
        name in names for name in ("build_image", "deploy_tier", "health_check", "smoke_test")
    )
    assert rig.scans and all(scan["source"] == "fixture" for scan in rig.scans)
    return rid, release


@pytest.mark.anyio
async def test_ai_patch_approval_git_release_off_reuse_and_source_reproposal(rig):
    try:
        first, release = await deploy(rig)
        assert len(rig.provider.requests) == 1
        assert rig.bundles[0].facts.patch_targets[0].pattern_id == "secret_key"
        assert "SECRET_KEY" in {key.name for key in rig.bundles[0].facts.env_keys if key.required}
        rig.toggle(False)
        reused, reuse_release = await deploy(rig)
        assert len(rig.provider.requests) == 1
        assert rig.service.approval_view(reused)["patch_meta"]["reuse"] is True
        assert reuse_release["source"] == release["source"]
        assert reuse_release["images"] == release["images"]
        assert rig.bundles[-1].facts.code_patch is False
        rig.update_prod()
        blocked = await rig.prepare()
        blocked_run = rig.service.get_run(blocked)
        assert blocked_run["status"] == "FAILED_BEFORE_DEPLOY", blocked_run
        assert "PRECONDITION_FAILED" in str(blocked_run["result"]), blocked_run
        assert len(rig.provider.requests) == 1
        assert not any(ctx.run_id == blocked for _, ctx in rig.calls)
        assert rig.service.store.environments("demo")["local"]["current"]["release_id"] == reused
        rig.toggle(True)
        changed, changed_release = await deploy(rig)
        assert len(rig.provider.requests) == 2
        assert rig.provider.requests[0]["targets"][0]["line"] == 2
        assert rig.provider.requests[1]["targets"][0]["line"] == 3
        assert rig.service.approval_view(changed)["patch_meta"]["reuse"] is False
        assert changed_release["source_sha"] != release["source_sha"]
        assert changed_release["images"] != release["images"]
        assert (rig.developer / APP_FILE).read_text() == UPDATED
        assert first != reused != changed
    finally:
        await rig.service.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("binding_mode", ["original", "patched"])
async def test_build_rejects_snapshot_without_approved_source_patch_binding(rig, binding_mode):
    try:
        rig.binding_mode = binding_mode
        rid = await rig.prepare()
        assert rig.service.get_run(rid)["status"] == "AWAITING_APPROVAL", rig.service.get_run(rid)
        rig.service.approve(rid, approver="fixture-operator")
        rig.service.start(rid)
        result = await rig.service.wait(rid)
        assert result.status is not RunStatus.SUCCEEDED
        assert any(record.error and "스냅샷" in str(record.error) for record in result.records), [
            (r.step_id, r.error) for r in result.records
        ]
        assert not any(name == "deploy_tier" for name, _ in rig.calls)
        assert not rig.service.store.environments("demo").get("local", {}).get("current")
        assert git(rig.bare, "rev-parse", "main") == rig.source_sha
    finally:
        await rig.service.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("directory", ["aaa", "zzz"])
async def test_two_file_patch_off_reuses_same_tree_regardless_of_diff_order(
    rig, monkeypatch, directory
):
    """OFF diff 순서를 fixture에서 명시적으로 선택해 정규화 구현과 독립적으로 검증한다."""
    extra_file = f"was/{directory}/settings.py"
    extra = rig.developer / extra_file
    extra.parent.mkdir()
    extra.write_text("import os\nAPP_BASE_URL = 'http://localhost:5000'\n")
    rig.provider.targets[extra_file] = "local_address"
    git(rig.developer, "add", extra_file)
    git(rig.developer, "commit", "-m", "Second configuration file fixture")
    git(rig.developer, "push", "origin", "prod", "HEAD:main", "HEAD:ai-prod")
    rig.source_sha = git(rig.developer, "rev-parse", "HEAD")
    monkeypatch.setenv("DDAK_ONPREM_INVENTORY", "fixture-inventory")
    monkeypatch.setattr(
        app,
        "load_inventory",
        lambda path: {
            "public_url": "https://fixture.invalid",
            "tiers": {"was": {"public_env": {"APP_BASE_URL": "https://fixture.invalid"}}},
        },
    )
    try:
        first, release = await deploy(rig)
        assert len(rig.provider.requests) == 1
        assert {t["file"] for t in rig.provider.requests[0]["targets"]} == {APP_FILE, extra_file}
        assert list(release["patch_ledger"]) == [APP_FILE, extra_file]
        real_prepare = app.prepare_patch

        def prepare_with_fixture_order(source, facts, ctx, **kwargs):
            result = real_prepare(source, facts, ctx, **kwargs)
            if facts.code_patch:
                return result
            canonical, changed = reuse_patches(source, kwargs["previous"], kwargs["runs_root"])
            assert canonical and not changed
            sections = canonical.split(b"--- a/")
            assert len(sections) == 3 and sections[0] == b""
            patch = canonical
            if directory == "aaa":
                patch = b"".join(b"--- a/" + section for section in reversed(sections[1:]))
                assert patch != canonical
            assert (
                preview(source, patch).build_snapshot_hash
                == preview(source, result.patch).build_snapshot_hash
            )
            return replace(
                result,
                patch=patch,
                meta={
                    **result.meta,
                    "patch_sha256": digest_bytes(patch),
                },
            )

        monkeypatch.setattr(app, "prepare_patch", prepare_with_fixture_order)
        rig.toggle(False)
        reused, reuse_release = await deploy(rig)
        prepared = rig.service._load_prepared(reused)
        sorted_patch, changed = reuse_patches(
            prepared.source, {"local": release}, rig.service.root / "runs"
        )
        assert changed == [] and sorted_patch
        assert (prepared.patch != sorted_patch) is (directory == "aaa")
        assert preview(prepared.source, sorted_patch).build_snapshot_hash == (
            prepared.snapshot.build_snapshot_hash
        )
        assert len(rig.provider.requests) == 1  # OFF에서 새 LLM 호출 없음
        assert rig.service.approval_view(reused)["patch_meta"]["reuse"] is True
        for key in ("source_snapshot_hash", "build_snapshot_hash"):
            assert reuse_release["source"][key] == release["source"][key]
        assert (reuse_release["source"]["patch_sha256"] != release["source"]["patch_sha256"]) is (
            directory == "aaa"
        )
        assert reuse_release["images"] == release["images"]
        assert reuse_release["patch_ledger"] == release["patch_ledger"]
        build = next(
            ctx for name, ctx in rig.calls if name == "build_image" and ctx.run_id == reused
        )
        patched = Path(build.build_source) / extra_file
        key_read = ast.parse(patched.read_text()).body[1].value
        assert ast.unparse(key_read) == "os.environ['APP_BASE_URL']"
        assert (
            git(rig.bare, "show", f"{build.candidate_sha}:{extra_file}")
            == patched.read_text().strip()
        )
        assert extra.read_text() == "import os\nAPP_BASE_URL = 'http://localhost:5000'\n"
        assert rig.service.store.environments("demo")["local"]["current"]["release_id"] == reused
        assert first != reused
    finally:
        await rig.service.shutdown()


@pytest.mark.anyio
async def test_admin_only_setup_reaches_patch_build_and_release(rig):
    """모든 제품 설정은 관리자 POST. 외부 작업은 fixture, 실제 터미널 명령 입력 0회."""
    from fastapi.testclient import TestClient

    from ddak.web.app import create_app
    from ddak.web.routes.setup import router
    from tests.unit.test_setup_service_fix11 import Builder

    manager = app._setup_service(rig.service, rig.settings, "127.0.0.1")
    manager._build_factory = Builder
    manager._test_provider = lambda *args, **kwargs: {"status": "green", "detail": "source=fixture"}
    rig.service.onboarding = manager
    rig.service.build_preflight = lambda ctx: ["source=fixture: managed local builder"]
    web = create_app(settings=rig.settings)
    web.state.deployment = rig.service
    web.include_router(router)
    client = TestClient(web, base_url="http://127.0.0.1:8765")
    try:
        response = client.get("/setup?project=demo")
        assert response.status_code == 200
        token = client.cookies["ddak_csrf"]

        def post(route, **data):
            out = client.post(
                "/setup/" + route,
                data={"project": "demo", "csrf_token": token, **data},
                headers={"origin": "http://127.0.0.1:8765"},
                follow_redirects=False,
            )
            assert out.status_code == 303, out.text

        version = rig.service.get_project_settings("demo")["version"]
        post(
            "choices",
            version=str(version),
            generation_provider="claude-api",
            judgment_provider="claude-api",
            generation_model="claude-sonnet-5-5",
            judgment_model="claude-sonnet-5-5",
            llm_effort="low",
            build_backend="local",
            image_repository="2026gerbera/flaskr",
        )
        post("key", provider_id="claude-api", value="fixture-private-value")
        post("test", provider_id="claude-api")
        post("build-apply", plan_hash=manager.build_plan("demo")["hash"])
        post("docker", username="fixture", token="fixture-private-token")
        post("env", key="APP_ENV", value="production")
        page = client.get("/setup?project=demo")
        assert "fixture-private-value" not in page.text
        assert "fixture-private-token" not in page.text
        rid, release = await deploy(rig)
        ctx = next(ctx for name, ctx in rig.calls if name == "build_image" and ctx.run_id == rid)
        assert ctx.build_backend == "local"
        assert ctx.image_repository == "2026gerbera/flaskr"
        assert ctx.platform["local_build"]["builder"] == "fixture-builder"
        assert release["patch_ledger"]
    finally:
        client.close()
        await rig.service.shutdown()
