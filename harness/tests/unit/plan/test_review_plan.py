"""source=fixture: 로컬 패치 재계획. Git fetch·AI 외부 호출·배포는 실행하지 않는다."""

from __future__ import annotations

import difflib
import socket
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from ddak.core import runtime
from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.ai.providers.jev import JevAnswer, JevQuestion
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.enums import LLMBackend, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan_facts import EnvKey, FileMeta
from ddak.core.snapshots import digest_bytes, digest_json, file_manifest, preview
from ddak.plan import replan_patch
from ddak.plan import review as review_mod
from ddak.plan.detect import facts_reader

SETTINGS = Settings(ai_retries=0, llm_backend=LLMBackend.REPLAY)
ORIGINAL = 'import os\nHOST = os.environ.get("DB_HOST")\n'
PATCHED = ORIGINAL + 'KEY = os.environ["SECRET_KEY"]\nSECURE = os.getenv("SESSION_COOKIE_SECURE")\n'


class FakeJev:
    source = Source.FIXTURE

    def ask(self, *, state: str, questions: Sequence[JevQuestion]) -> list[JevAnswer]:
        assert runtime.current_tool.get() in {"analyze_project", "generate_plan"}
        return [JevAnswer(id=q.id, probability=0.1) for q in questions]


class FakeProvider:
    name = "replay"

    def complete(self, req: AIRequest) -> AIResponse:
        assert req.purpose == "generate_plan"
        return AIResponse(text='{"decisions": []}', source=Source.FIXTURE)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("외부 네트워크 호출 금지")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "original"
    files = {
        "deploy.yaml": (
            "tiers:\n  web:\n    paths: [web]\n    dockerfile: web/Dockerfile\n"
            "  was:\n    paths: [was]\n    dockerfile: null\nenv_example: env.names\n"
        ),
        "web/Dockerfile": "FROM nginx\n",
        "was/app.py": ORIGINAL,
        "env.names": "DB_HOST=\n",
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


@pytest.fixture
def copies(tmp_path, monkeypatch):
    paths = []

    def tracked(**kwargs):
        directory = TemporaryDirectory(dir=tmp_path, **kwargs)
        paths.append(Path(directory.name))
        return directory

    monkeypatch.setattr(review_mod, "TemporaryDirectory", tracked)
    return paths


def patch_for(text=PATCHED):
    return "".join(
        difflib.unified_diff(
            ORIGINAL.splitlines(True), text.splitlines(True), "a/was/app.py", "b/was/app.py"
        )
    ).encode()


def go(source, patch=None, **kwargs):
    manifest = {k: FileMeta(**v) for k, v in file_manifest(source).items()}
    request = kwargs.pop(
        "request",
        DeployRequest(
            project="demo", repo_url="https://example.invalid/repo", target="both", code_patch=True
        ),
    )
    kwargs.setdefault("previous_manifests", lambda _: {"local": manifest, "cloud": manifest})
    kwargs.setdefault("jev_client", FakeJev())
    kwargs.setdefault("provider", FakeProvider())
    return replan_patch(
        request, run_id="run-review", source=source, patch=patch, settings=SETTINGS, **kwargs
    )


def test_patch_adds_required_keys_and_builds_previously_unchanged_tier(source, copies):
    before = file_manifest(source)
    bundle = go(source, patch_for(), cloud_domain="app.example.com", platform={"cloud": {}})
    assert bundle.facts.changed == {
        "local": {"web": False, "was": True},
        "cloud": {"web": False, "was": True},
    }
    keys = {key.name: key for key in bundle.facts.env_keys}
    assert keys["SECRET_KEY"].is_new and keys["SECRET_KEY"].kind == "secret"
    assert keys["SESSION_COOKIE_SECURE"].is_new
    assert keys["SESSION_COOKIE_SECURE"].kind == "plain"
    assert keys["SECRET_KEY"].tier == "was"
    assert bundle.facts.infra_inputs_changed
    assert bundle.facts.has_dockerfile == {"web": True, "was": False}
    assert [s.id for s in bundle.plan.build.steps] == ["build.was"]
    cloud = {s.id: s for s in bundle.plan.deploy.cloud.steps}
    local = {s.id: s for s in bundle.plan.deploy.local.steps}
    assert "deploy.was.cloud" in cloud and "deploy.was.local" in local
    assert "deploy.infra.cloud" in cloud
    assert cloud["deploy.secrets.cloud"].params["keys"] == ["SECRET_KEY"]
    for steps in (local, cloud):
        config = next(s for s in steps.values() if s.tool == "inject_env_config")
        assert config.params["keys"] == ["SECRET_KEY"]
    assert keys["SECRET_KEY"].required
    assert not keys["SESSION_COOKIE_SECURE"].required
    original_hash = digest_json(before)
    assert bundle.facts.facts_hash == bundle.plan.facts_hash == facts_reader(source)
    assert bundle.facts.source_snapshot_hash == original_hash
    assert bundle.context.source_binding is None
    assert bundle.context.cloud_domain == "app.example.com"
    assert bundle.context.platform == {"cloud": {}}
    assert bundle.context.toggles == bundle.plan.toggles
    binding = preview(source, patch_for())
    assert binding.source_snapshot_hash == original_hash
    assert binding.build_snapshot_hash != original_hash
    assert binding.patch_sha256 == digest_bytes(patch_for())
    assert bundle.source == source and file_manifest(source) == before
    assert copies and all(not path.exists() for path in copies)
    assert runtime.current_tool.get() is None


def test_patch_matching_previous_success_accepts_existing_keys_without_reinjection(source, copies):
    before = file_manifest(source)
    previous = {path: FileMeta(**meta) for path, meta in before.items()}
    previous["was/app.py"] = previous["was/app.py"].model_copy(
        update={"sha256": digest_bytes(PATCHED.encode())}
    )
    bundle = go(
        source,
        patch_for(),
        previous_manifests=lambda _: {"local": previous, "cloud": previous},
    )
    assert bundle.facts.changed == {
        "local": {"web": False, "was": False},
        "cloud": {"web": False, "was": False},
    }
    assert {key.name: key.is_new for key in bundle.facts.env_keys} == {
        "DB_HOST": False,
        "SECRET_KEY": False,
        "SESSION_COOKIE_SECURE": False,
    }
    assert not bundle.plan.build.steps
    assert not bundle.facts.infra_inputs_changed
    for section in (bundle.plan.deploy.local, bundle.plan.deploy.cloud):
        assert not any(
            step.tool in {"inject_env_config", "sync_env_to_cloud", "apply_infra", "deploy_tier"}
            for step in section.steps
        )
        assert {step.tool for step in section.steps} >= {"health_check", "smoke_test"}
    assert bundle.plan.facts_hash == bundle.facts.facts_hash == digest_json(before)
    assert bundle.facts.source_snapshot_hash == digest_json(before)
    assert bundle.source == source and file_manifest(source) == before
    assert copies and all(not path.exists() for path in copies)


def test_deselection_replans_original_without_previous_patch_effects(source, copies):
    first = go(source, patch_for())
    second = go(source)
    assert first.facts.infra_inputs_changed
    assert not second.facts.infra_inputs_changed
    assert {key.name for key in second.facts.env_keys} == {"DB_HOST"}
    assert not second.plan.build.steps
    assert not any(any(tiers.values()) for tiers in second.facts.changed.values())
    assert first.facts.facts_hash == second.facts.facts_hash
    assert all(not path.exists() for path in copies)


def test_deselecting_deployed_patch_rebuilds_and_requires_original_env_keys(source, copies):
    before = file_manifest(source)
    original = {path: FileMeta(**meta) for path, meta in before.items()}
    patched = {
        **original,
        "was/app.py": original["was/app.py"].model_copy(
            update={"sha256": digest_bytes(PATCHED.encode())}
        ),
    }
    calls = []

    def deployed(project):
        calls.append(project)
        return {env: {"web": original, "was": patched} for env in ("local", "cloud")}

    bundle = go(source, patch=None, previous_tier_manifests=deployed)
    assert calls == ["demo"]
    assert bundle.facts.changed == {
        "local": {"web": False, "was": True},
        "cloud": {"web": False, "was": True},
    }
    assert [step.id for step in bundle.plan.build.steps] == ["build.was"]
    assert {key.name: key.is_new for key in bundle.facts.env_keys} == {"DB_HOST": True}
    for env in ("local", "cloud"):
        steps = {step.id: step for step in getattr(bundle.plan.deploy, env).steps}
        assert f"deploy.was.{env}" in steps
        assert not any(
            step.tool in {"inject_env_config", "sync_env_to_cloud"} for step in steps.values()
        )
    assert not bundle.facts.infra_inputs_changed
    assert bundle.facts.source_snapshot_hash == bundle.plan.facts_hash == digest_json(before)
    assert patched["was/app.py"].sha256 == digest_bytes(PATCHED.encode())
    assert file_manifest(source) == before
    assert copies and all(not path.exists() for path in copies)


def test_partial_carry_uses_each_environment_and_tiers_actual_release_files(source):
    current = {path: FileMeta(**meta) for path, meta in file_manifest(source).items()}
    older_release = {
        **current,
        "was/app.py": current["was/app.py"].model_copy(
            update={"sha256": digest_bytes(PATCHED.encode())}
        ),
        "web/Dockerfile": current["web/Dockerfile"].model_copy(
            update={"sha256": digest_bytes(b"FROM older-image\n")}
        ),
    }
    # local WAS만 옛 release의 이미지를 이월했다. 그 release의 WEB 파일은 비교하면 안 된다.
    bundle = go(
        source,
        previous_tier_manifests=lambda _: {
            "local": {"web": current, "was": older_release},
            "cloud": {"web": current, "was": current},
        },
    )
    assert bundle.facts.changed == {
        "local": {"web": False, "was": True},
        "cloud": {"web": False, "was": False},
    }
    assert [step.id for step in bundle.plan.build.steps] == ["build.was"]


@pytest.mark.parametrize("reverse", [False, True])
def test_overlapping_tier_paths_keep_distinct_image_manifests(source, reverse):
    (source / "deploy.yaml").write_text(
        "tiers:\n  web:\n    paths: [shared]\n  was:\n    paths: [shared]\n"
    )
    (source / "shared").mkdir()
    (source / "shared/app.py").write_text("VERSION = 2\n")
    current = {path: FileMeta(**meta) for path, meta in file_manifest(source).items()}
    old = {
        **current,
        "shared/app.py": current["shared/app.py"].model_copy(
            update={"sha256": digest_bytes(b"VERSION = 1\n")}
        ),
    }
    entries = [("web", current), ("was", old)]
    if reverse:
        entries.reverse()
    bundle = go(
        source,
        previous_tier_manifests=lambda _: {env: dict(entries) for env in ("local", "cloud")},
    )
    assert bundle.facts.changed == {
        "local": {"web": False, "was": True},
        "cloud": {"web": False, "was": True},
    }
    assert [step.id for step in bundle.plan.build.steps] == ["build.was"]


def test_effective_comparison_preserves_source_changes_migrations_and_db_baseline(source):
    with (source / "deploy.yaml").open("a") as stream:
        stream.write("migrations_dir: migrations\n")
    (source / "migrations").mkdir()
    (source / "migrations/0001_init.sql").write_text("select 1;\n")
    (source / "migrations/0002_next.sql").write_text("select 2;\n")
    current = {path: FileMeta(**meta) for path, meta in file_manifest(source).items()}
    previous_source = {
        path: meta for path, meta in current.items() if path != "migrations/0002_next.sql"
    }
    for path in ("was/app.py", "migrations/0001_init.sql"):
        previous_source[path] = previous_source[path].model_copy(
            update={"sha256": digest_bytes(b"older source\n")}
        )
    bundle = go(
        source,
        previous_manifests=lambda _: {env: previous_source for env in ("local", "cloud")},
        previous_tier_manifests=lambda _: {
            env: {"web": current, "was": current} for env in ("local", "cloud")
        },
    )
    assert all(changed["was"] for changed in bundle.facts.changed.values())
    assert bundle.facts.new_migrations == ("0002",)
    assert bundle.facts.modified_migrations == ("0001",)
    assert bundle.facts.db_initialized == {"local": True, "cloud": True}
    assert bundle.facts.source_snapshot_hash == bundle.plan.facts_hash == facts_reader(source)


@pytest.mark.parametrize("target", ["local", "cloud", "both"])
def test_target_scope_and_bootstrap_observations(source, target):
    request = DeployRequest(
        project="demo", repo_url="https://example.invalid/repo", target=target, code_patch=True
    )
    bundle = go(source, patch_for(), request=request, previous_manifests=lambda _: {})
    assert bundle.facts.db_initialized == {"local": False, "cloud": False}
    assert {s.id for s in bundle.plan.build.steps} == {"build.web", "build.was"}
    if target == "local":
        assert not bundle.plan.deploy.cloud.steps
    if target == "cloud":
        assert not bundle.plan.deploy.local.steps


@pytest.mark.parametrize("patch", [b"", b"invalid", patch_for().replace(b"DB_HOST", b"OTHER_HOST")])
def test_unapplicable_patch_preserves_source_and_cleans_temp(source, copies, patch):
    before = file_manifest(source)
    with pytest.raises(DdakToolError) as exc:
        go(source, patch)
    assert exc.value.code is ErrorCode.PRECONDITION_FAILED
    assert file_manifest(source) == before
    assert copies and all(not path.exists() for path in copies)


@pytest.mark.parametrize(
    "stage", ["detect_changed_tiers", "analyze_project", "generate_plan", "validate_plan"]
)
def test_stage_failure_cleans_only_temp(source, copies, monkeypatch, stage):
    before = file_manifest(source)
    stages = []

    def fail(*args, **kwargs):
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(review_mod, stage, fail)
    with pytest.raises(RuntimeError, match="fixture failure"):
        go(source, patch_for(), record_stage=lambda *event: stages.append(event))
    assert file_manifest(source) == before
    assert copies and all(not path.exists() for path in copies)
    assert runtime.current_tool.get() is None
    order = ["detect_changed_tiers", "analyze_project", "generate_plan", "validate_plan"]
    names = ["detect", "analyze", "plan", "validate"][: order.index(stage) + 1]
    assert [(name, status) for name, _, status in stages] == [
        (name, "failed" if name == names[-1] else "succeeded") for name in names
    ]
    assert all(isinstance(ms, int) and ms >= 0 for _, ms, _ in stages)


def test_rule_fallback_uses_patched_facts_with_original_binding(source, monkeypatch):
    validate = review_mod.validate_plan
    calls = []

    def reject_first(inp, ctx):
        calls.append(inp)
        if inp.draft is not None:
            raise DdakToolError(ErrorCode.PLAN_INVALID, "fixture invalid draft")
        return validate(inp, ctx)

    monkeypatch.setattr(review_mod, "validate_plan", reject_first)
    bundle = go(source, patch_for())
    assert len(calls) == 2 and calls[-1].draft is None
    assert bundle.plan.planner.fallback
    assert bundle.plan.facts_hash == facts_reader(source)
    assert "SECRET_KEY" in {key.name for key in bundle.facts.env_keys}


def test_source_changed_during_replanning_is_rejected(source, copies, monkeypatch):
    generate = review_mod.generate_plan

    def concurrent_change(*args, **kwargs):
        (source / "was/app.py").write_text(ORIGINAL + "# concurrent change\n")
        return generate(*args, **kwargs)

    monkeypatch.setattr(review_mod, "generate_plan", concurrent_change)
    with pytest.raises(DdakToolError) as exc:
        go(source, patch_for())
    assert exc.value.code is ErrorCode.PRECONDITION_FAILED
    assert copies and all(not path.exists() for path in copies)


def test_toggle_off_rejects_patch_but_allows_no_patch(source, copies):
    request = DeployRequest(project="demo", repo_url="https://example.invalid/repo", target="local")
    with pytest.raises(DdakToolError) as exc:
        go(source, patch_for(), request=request)
    assert exc.value.code is ErrorCode.TOGGLE_OFF
    assert not copies
    assert not go(source, request=request).context.toggles["code_patch"]


def test_symlink_source_rejected(source, tmp_path, copies):
    link = tmp_path / "source-link"
    link.symlink_to(source, target_is_directory=True)
    with pytest.raises(DdakToolError) as exc:
        replan_patch(
            DeployRequest(project="demo", repo_url="https://example.invalid/repo", target="local"),
            run_id="run-review",
            source=link,
            patch=None,
            settings=SETTINGS,
            previous_manifests=lambda _: {},
        )
    assert exc.value.code is ErrorCode.PRECONDITION_FAILED
    assert not copies


@pytest.mark.parametrize("read", ['getenv("NEW_KEY")', 'env_bool("FEATURE_ENABLED")'])
def test_env_reads_missed_by_existing_analyzer_fail_closed(source, copies, read):
    before = file_manifest(source)
    with pytest.raises(DdakToolError) as exc:
        go(source, patch_for(ORIGINAL + f"VALUE = {read}\n"))
    assert exc.value.code is ErrorCode.PLAN_INVALID
    assert file_manifest(source) == before
    assert copies and all(not path.exists() for path in copies)


def test_injected_provider_fallback_never_uses_external_ai(source):
    class UnavailableJev:
        def ask(self, **kwargs):
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "fixture unavailable")

    bundle = go(source, patch_for(), jev_client=UnavailableJev())
    assert bundle.plan.planner.source is Source.FIXTURE
    assert bundle.plan.planner.provider == "replay"


def test_spaced_getenv_is_analyzed_without_losing_required_subscript(source, copies):
    before = file_manifest(source)
    bundle = go(
        source,
        patch_for(ORIGINAL + 'VALUE = os.getenv ( "NEW_KEY" )\nKEY = os.environ["SECRET_KEY"]\n'),
    )
    keys = {key.name: key for key in bundle.facts.env_keys}
    assert keys["NEW_KEY"].is_new and not keys["NEW_KEY"].required
    assert keys["SECRET_KEY"].is_new and keys["SECRET_KEY"].required
    assert bundle.plan.facts_hash == digest_json(before)
    assert file_manifest(source) == before
    assert copies and all(not path.exists() for path in copies)


@pytest.mark.parametrize("trigger", ["manual", "auto"])
def test_source_context_and_stage_records_survive_replanning(source, trigger):
    original = RunContext(
        "parent-run",
        project="demo",
        trigger=trigger,
        targets="both",
        repo_url="https://example.invalid/repo",
        ref="prod",
        source_sha="a" * 40,
        project_settings={"version": 7},
        image_repository="example.test/demo",
        build_backend="local",
    )
    before = original.to_json_dict()
    stages = []
    bundle = go(
        source,
        patch_for(),
        source_context=original,
        record_stage=lambda *event: stages.append(event),
    )
    assert bundle.context.run_id == "run-review"
    for name in (
        "trigger",
        "targets",
        "repo_url",
        "ref",
        "source_sha",
        "project_settings",
        "image_repository",
        "build_backend",
    ):
        assert getattr(bundle.context, name) == getattr(original, name)
    assert bundle.context.toggles["code_patch"]
    assert original.to_json_dict() == before
    assert [(name, status) for name, _, status in stages] == [
        (name, "succeeded") for name in ("detect", "analyze", "plan", "validate")
    ]
    assert all(isinstance(ms, int) and ms >= 0 for _, ms, _ in stages)


def test_patch_env_metadata_merges_requiredness_without_losing_observed_tier(source):
    metadata = (
        EnvKey(name="SESSION_COOKIE_SECURE", kind="plain", required=True, is_new=False),
        EnvKey(name="SECRET_KEY", kind="plain", required=False, tier="web", is_new=False),
        EnvKey(name="OPTIONAL_HINT", kind="plain", required=False, is_new=False),
    )
    bundle = go(source, patch_for(), patch_env_keys=metadata)
    keys = {key.name: key for key in bundle.facts.env_keys}
    assert len(keys) == len(bundle.facts.env_keys)
    assert keys["SESSION_COOKIE_SECURE"].required
    assert keys["SECRET_KEY"].kind == "plain" and not keys["SECRET_KEY"].required
    assert keys["SECRET_KEY"].tier == "was" and keys["SECRET_KEY"].is_new
    assert keys["OPTIONAL_HINT"] == metadata[-1]
    assert metadata[0].is_new is False and metadata[1].tier == "web"


def test_replanning_preserves_smoke_groups_and_unselected_patch_targets(source):
    (source / "web/index.html").write_text('<div class="release-box">v2</div>\n')
    (source / "web/config.py").write_text("SESSION_COOKIE_SECURE = False\n")
    (source / "was/config.py").write_text('SECRET_KEY = "dev"\n')
    before = file_manifest(source)
    bundle = go(source, patch_for())
    assert bundle.facts.smoke_groups == ("v2",)
    assert {
        (t.file, t.line, t.pattern_id, t.is_new, t.severity) for t in bundle.facts.patch_targets
    } == {
        ("web/config.py", 1, "cookie_secure", False, "patch"),
        ("was/config.py", 1, "secret_key", False, "patch"),
    }
    for section in (bundle.plan.deploy.local, bundle.plan.deploy.cloud):
        smoke = [step for step in section.steps if step.tool == "smoke_test"]
        assert smoke and all(step.params["scenarios"] == ["base", "v2"] for step in smoke)
    assert file_manifest(source) == before
