"""source=fixture: 등록 패치 툴→재계획→승인 조립. AI·배포는 외부 실행 없음."""

import json
import socket
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ddak import app as assembly
from ddak.core import runtime
from ddak.core.ai import gateway
from ddak.core.ai.providers import AIResponse
from ddak.core.config import LLMBackend, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Plan
from ddak.core.contracts.plan_facts import FileMeta
from ddak.core.contracts.tools.patch_config import PatchViolation
from ddak.core.patch_ledger import file_diff, save_ledger
from ddak.core.registry import Registry, spec_for
from ddak.core.snapshots import digest_bytes, digest_json
from ddak.executor.engine import RunStatus
from ddak.plan.patch import pipeline
from ddak.plan.patch import tool as patch_tool
from tests.unit import test_deployment_service as support
from tests.unit.test_approval_meta import HASH, summary
from tests.unit.test_ui_integration_fix10 import client_for, post
from tests.unit.test_ui_patch_review import COOKIE, ORIGINAL, begin, wait_review

rig = support.rig


@pytest.fixture(autouse=True)
def no_external_execution(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("외부 네트워크 호출 금지")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    popen = subprocess.Popen

    def local_patch_only(args, *rest, **kwargs):
        assert not isinstance(args, str) and list(args[:2]) == ["git", "apply"], args
        return popen(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_patch_only)


class FixtureProvider:
    name = "replay"

    def __init__(self):
        self.requests = []
        self.intents = [
            {"file": "app.py", "line": 2, "pattern_id": "secret_key", "key": "SECRET_KEY"}
        ]

    def complete(self, request):
        assert runtime.current_tool.get() == "patch_config"
        assert request.purpose == "patch_config"
        self.requests.append(request)
        return AIResponse(
            text=json.dumps({"intents": self.intents, "reason": "fixture 설정 검토"}),
            source=Source.FIXTURE,
        )


@pytest.fixture
def registered_review(rig, monkeypatch):
    service, source, calls = rig
    (source / "app.py").write_text(ORIGINAL)
    original_registry = service.registry
    registry = Registry((*original_registry.specs, spec_for("patch_config")))
    for name in original_registry.registered():
        registry.tool(name)(original_registry.get(name).fn)
    registry.tool("patch_config")(patch_tool.patch_config)
    service.registry = registry
    provider = FixtureProvider()
    harness = SimpleNamespace(
        service=service,
        source=source,
        calls=calls,
        provider=provider,
        tools=[],
        scans=[],
        replans=[],
        infra=[],
        corrupt={},
    )
    monkeypatch.setattr(gateway, "get_provider", lambda settings: provider)
    monkeypatch.setattr(
        pipeline, "strict_patch_scan", lambda root, patch: harness.scans.append(patch)
    )
    real_patch = patch_tool._patch_config

    def observed_patch(inp, ctx):
        assert runtime.current_run_id.get() == ctx.run_id
        output = real_patch(inp, ctx)
        output = output.model_copy(update=harness.corrupt.get(inp.review.action, {}))
        harness.tools.append((inp, ctx, output))
        return output

    monkeypatch.setattr(patch_tool, "_patch_config", observed_patch)

    def replan(request, **kwargs):
        harness.replans.append((request, kwargs))
        plan = support.plan(kwargs["run_id"], patch=bool(kwargs["patch"]))
        return SimpleNamespace(
            plan=plan,
            context=replace(kwargs["source_context"], toggles=plan.toggles),
            facts=SimpleNamespace(env_keys=kwargs["patch_env_keys"]),
        )

    async def infra(service, plan, context):
        harness.infra.append((plan, context))
        return {}, None

    monkeypatch.setattr(assembly, "replan_patch", replan)
    monkeypatch.setattr(assembly, "_infra_approval", infra)
    assembly._configure_patch_review(service, Settings(llm_backend=LLMBackend.REPLAY, ai_retries=0))
    return harness


def prepare_review_parent(harness, **overrides):
    plan = support.plan("review-parent")
    context = replace(
        RunContext(
            plan.run_id,
            project=plan.project,
            repo_url="https://example.invalid/app.git",
            trigger="auto",
            targets="both",
            ref="prod",
            source_sha="a" * 40,
        ),
        **overrides,
    )
    harness.service.prepare(plan, context, harness.source)
    return context


def finalize(client, reviews, draft, selected=None):
    selected = draft["selected"] if selected is None else selected
    response = post(
        client,
        "/runs/review-parent/patch-review",
        action="finalize",
        revision=draft["revision"],
        **{f"apply_{key}": "on" for key in selected},
    )
    assert response.status_code == 303
    wait_review(client, reviews)
    return reviews.get("review-parent")


@pytest.mark.parametrize("settings_changed", [False, True])
def test_assembly_replans_selected_patch_with_fresh_infra_subjects(
    registered_review, monkeypatch, settings_changed
):
    harness = registered_review
    service, source, calls = harness.service, harness.source, harness.calls
    context = prepare_review_parent(harness)

    async def infra(service, plan, context):
        harness.infra.append((plan, context))
        assert len(harness.replans) == 1
        if settings_changed:
            service.save_project_settings(
                context.project, {}, updated_by="fixture", expected_version=0
            )
        return {"infra": HASH}, summary()

    monkeypatch.setattr(assembly, "_infra_approval", infra)
    reviews = service.patch_reviews
    with client_for(service) as client:
        draft = begin(client, reviews)
        assert draft["state"] == "ready", draft
        initial = service.store.prepared("review-parent")
        draft = finalize(client, reviews, draft)
        assert [inp.review.action for inp, _, _ in harness.tools] == ["propose", "compose"]
        assert service.registry.get("patch_config").fn is patch_tool.patch_config
        assert len(harness.replans) == len(harness.infra) == 1
        request, kwargs = harness.replans[0]
        assert request.code_patch is True and request.ref == context.source_sha
        assert kwargs["source"] != source and (kwargs["source"] / "app.py").read_text() == ORIGINAL
        assert kwargs["source_context"].trigger == "auto"
        assert {key.name for key in kwargs["patch_env_keys"]} == {"SECRET_KEY"}
        assert callable(kwargs["record_stage"])
        assert service.store.prepared("review-parent") == initial
        if settings_changed:
            assert draft["state"] == "ready" and draft["successor"] is None
            assert "프로젝트 설정" in draft["error"]
            assert service.get_run("review-parent")["status"] == "AWAITING_APPROVAL"
        else:
            child = draft["successor"]
            infra_plan, infra_context = harness.infra[0]
            assert child != "review-parent"
            assert child == kwargs["run_id"] == infra_plan.run_id == infra_context.run_id
            prepared = service._load_prepared(child)
            assert prepared.context.trigger == "auto"
            assert prepared.context.review_baseline_hash == digest_json(
                service.store.environments(context.project)
            )
            assert prepared.patch == kwargs["patch"] == harness.tools[-1][2].patch.encode()
            metadata = json.loads(prepared.patch_meta_json)
            assert metadata["passed"] is True
            assert metadata["patch_sha256"] == digest_bytes(prepared.patch)
            assert metadata["source"] == "cache"  # compose는 AI 재호출 없이 선택을 검사한다.
            assert prepared.requirements["infra"] == HASH
            assert json.loads(prepared.infra_summary_json) == summary()
            assert len(harness.provider.requests) == 1
            assert service.get_approvals(child) == []
            with pytest.raises(DdakToolError):
                service.approve("review-parent", approver="fixture")
            newer = support.plan("new-commit")
            service.prepare(
                newer,
                RunContext(
                    newer.run_id,
                    project=newer.project,
                    trigger="auto",
                    ref="prod",
                    repo_url=context.repo_url,
                    source_sha="b" * 40,
                ),
                source,
            )
            assert service.get_run(child)["status"] == "SUPERSEDED"
        assert not calls.contexts
        assert (source / "app.py").read_text() == ORIGINAL


def test_registered_revision_is_explicitly_adopted_before_composition(registered_review):
    harness = registered_review
    prepare_review_parent(harness)
    service, reviews = harness.service, harness.service.patch_reviews
    with client_for(service) as client:
        draft = begin(client, reviews)
        identity = draft["proposals"][0]["id"]
        response = post(
            client,
            "/runs/review-parent/patch-review",
            action=f"revise:{identity}",
            revision=draft["revision"],
            **{f"apply_{identity}": "on", f"prompt_{identity}": "서명 키 읽기 검토"},
        )
        assert response.status_code == 303
        wait_review(client, reviews)
        revised = reviews.get("review-parent")
        assert revised["candidate"] is not None, revised
        assert revised["proposals"] == draft["proposals"]
        assert revised["candidate"]["proposal"]["revision"] == 2
        response = post(
            client,
            "/runs/review-parent/patch-review",
            action="adopt",
            revision=revised["revision"],
            candidate_id=revised["candidate"]["id"],
            **{f"apply_{identity}": "on"},
        )
        assert response.status_code == 303
        final = finalize(client, reviews, reviews.get("review-parent"))
        assert final["successor"] is not None, final
        assert [inp.review.action for inp, _, _ in harness.tools] == [
            "propose",
            "revise",
            "compose",
        ]
        assert harness.tools[1][0].review.prompt == "서명 키 읽기 검토"
        assert harness.tools[-1][0].review.proposals[0].revision == 2
        assert len(harness.provider.requests) == 2
        assert not harness.calls.contexts


@pytest.mark.parametrize("phase", ["propose", "compose"])
@pytest.mark.parametrize("invalid", ["passed_false", "sha_mismatch", "sha_missing"])
def test_invalid_registered_verdict_cannot_reach_replan_or_approval(
    registered_review, phase, invalid
):
    harness = registered_review
    prepare_review_parent(harness)
    harness.corrupt[phase] = {
        "passed_false": {"passed": False},
        "sha_mismatch": {"patch_sha256": digest_bytes(b"different diff")},
        "sha_missing": {"patch_sha256": None},
    }[invalid]
    service, reviews = harness.service, harness.service.patch_reviews
    with client_for(service) as client:
        draft = begin(client, reviews)
        if phase == "compose":
            assert draft["state"] == "ready", draft
            draft = finalize(client, reviews, draft)
        assert draft["successor"] is None
        assert "패치 툴 판정과 승인 대상이 다르다" in draft["error"]
        assert not harness.replans and not harness.infra
        assert service.get_run("review-parent")["status"] == "AWAITING_APPROVAL"
        assert service.get_approvals("review-parent") == []
        assert not harness.calls.contexts
        assert (harness.source / "app.py").read_text() == ORIGINAL


@pytest.mark.parametrize("selected", [True, False])
def test_new_required_key_blocks_approval_and_deselection_removes_it(registered_review, selected):
    harness = registered_review
    (harness.source / "cookie.py").write_text(COOKIE)
    harness.provider.intents.append(
        {
            "file": "cookie.py",
            "line": 2,
            "pattern_id": "cookie_secure",
            "key": "SESSION_COOKIE_SECURE",
        }
    )
    prepare_review_parent(harness, required_env_keys=("OBSOLETE_KEY",))
    service, reviews = harness.service, harness.service.patch_reviews
    with client_for(service) as client:
        draft = begin(client, reviews)
        assert len(draft["proposals"]) == 2, draft
        chosen = [
            p["id"] for p in draft["proposals"] if selected or p["edits"][0]["path"] != "cookie.py"
        ]
        draft = finalize(client, reviews, draft, chosen)
        child = draft["successor"]
        assert child is not None, draft
        prepared = service._load_prepared(child)
        expected = {"SECRET_KEY", "SESSION_COOKIE_SECURE"} if selected else {"SECRET_KEY"}
        assert set(prepared.context.required_env_keys) == expected
        assert "OBSOLETE_KEY" not in prepared.context.required_env_keys
        assert service.approval_view(child)["missing_env_keys"] == (
            ["SESSION_COOKIE_SECURE"] if selected else []
        )
        if selected:
            with pytest.raises(DdakToolError, match="값 필요: SESSION_COOKIE_SECURE"):
                service.approve(child, approver="fixture")
            assert service.get_approvals(child) == []
        else:
            assert b"cookie.py" not in prepared.patch
            assert service.approve(child, approver="fixture")
        assert not harness.calls.contexts


@pytest.mark.parametrize("override", [None, "false"])
def test_required_public_values_derive_before_infra_and_preserve_explicit_values(
    registered_review, override
):
    harness = registered_review
    (harness.source / "cookie.py").write_text(COOKIE)
    harness.provider.intents.append(
        {
            "file": "cookie.py",
            "line": 2,
            "pattern_id": "cookie_secure",
            "key": "SESSION_COOKIE_SECURE",
        }
    )
    platform = {
        "onprem": {
            "public_url": "https://fixture.example",
            "tiers": {
                "was": {
                    "public_env": {} if override is None else {"SESSION_COOKIE_SECURE": override},
                }
            },
        }
    }
    before = json.loads(json.dumps(platform))
    prepare_review_parent(harness, platform=platform)
    service, reviews = harness.service, harness.service.patch_reviews
    with client_for(service) as client:
        draft = finalize(client, reviews, begin(client, reviews))
        child = draft["successor"]
        assert child is not None, draft
        prepared = service._load_prepared(child)
        for context in (harness.infra[0][1], prepared.context):
            assert context.platform["onprem"]["tiers"]["was"]["public_env"][
                "SESSION_COOKIE_SECURE"
            ] == (override or "true")
        assert platform == before
        assert service.approval_view(child)["missing_env_keys"] == []
        assert service.approve(child, approver="fixture")
        assert not harness.calls.contexts


@pytest.mark.parametrize("target", ["both", "cloud"])
def test_infra_failure_isolated_to_cloud_only_when_local_can_continue(
    registered_review, monkeypatch, target
):
    harness = registered_review
    prepare_review_parent(harness, targets=target)
    service, reviews = harness.service, harness.service.patch_reviews

    async def unavailable(service, plan, context):
        raise DdakToolError(ErrorCode.INFRA_MISSING, "fixture infra unavailable")

    monkeypatch.setattr(assembly, "_infra_approval", unavailable)
    with client_for(service) as client:
        draft = finalize(client, reviews, begin(client, reviews))
        child = draft["successor"]
        if target == "cloud":
            assert child is None and "fixture infra unavailable" in draft["error"]
            assert service.get_approvals("review-parent") == []
            assert not harness.calls.contexts
        else:
            assert child is not None, draft
            prepared = service._load_prepared(child)
            assert prepared.context.preparation_failures == {"cloud": ["apply_infra"]}
            assert prepared.context.preparation_errors == {
                "cloud": {
                    "phase": "infra",
                    "code": "INFRA_MISSING",
                    "detail": "fixture infra unavailable",
                }
            }
            assert "infra" not in prepared.requirements
            service.approve(child, approver="fixture")
            client.portal.call(service.start, child)
            result = client.portal.call(service.wait, child)
            assert result.status is RunStatus.FAILED_CLOUD
            assert result.tracks["local"] == "DONE"
            names = [name for name, _ in harness.calls.contexts]
            assert "deploy.local" in names and "smoke.local" in names
            assert not {"deploy.cloud", "smoke.cloud", "compare"}.intersection(names)
            assert service.store.project_state("demo")["lock"] is None


def test_actual_built_manifest_follows_carried_image_origin():
    old = {"was/app.py": {"sha256": digest_bytes(b"patched"), "executable": False}}
    current = {"was/app.py": {"sha256": digest_bytes(b"original"), "executable": False}}
    store = SimpleNamespace(
        environments=lambda project: {
            "local": {
                "current": {
                    "release_id": "latest",
                    "files": current,
                    "images": {"was": "image", "web": "image2"},
                    "image_sources": {
                        "was": {"release_id": "older", "carried_forward": True},
                        "web": {"release_id": "latest"},
                    },
                }
            }
        },
        release_record=lambda run_id: {"files": old} if run_id == "older" else None,
    )
    files = assembly._previous_built_tiers(store, "demo")
    assert files["local"]["was"]["was/app.py"] == FileMeta(**old["was/app.py"])
    assert files["local"]["web"]["was/app.py"] == FileMeta(**current["was/app.py"])


@pytest.mark.anyio
@pytest.mark.parametrize("boundary", ["approve", "start"])
async def test_review_without_build_rejects_an_intervening_patch_deployment(rig, boundary):
    service, source, calls = rig
    support.seed_releases(service)
    baseline = digest_json(service.store.environments("demo"))
    stale = Plan(run_id="review-no-build", project="demo")
    service.prepare(
        stale,
        RunContext(
            stale.run_id, project="demo", source_sha="a" * 40, review_baseline_hash=baseline
        ),
        source,
    )
    # 재시작에서도 읽는 DB 자료에 기준이 남고, 기존 이월 검사 대상은 비어 있다.
    persisted = service._load_prepared(stale.run_id)
    assert persisted.context.review_baseline_hash == baseline
    assert persisted.context.previous_release == {}
    if boundary == "start":
        service.approve(stale.run_id, approver="fixture")

    other = support.plan("intervening-patch", patch=True)
    service.prepare(
        other,
        RunContext(other.run_id, project="demo", source_sha="a" * 40, toggles=other.toggles),
        source,
        patch=support.PATCH,
        patch_meta=support.patch_metadata(),
    )
    service.approve(other.run_id, approver="fixture")
    service.start(other.run_id)
    assert (await service.wait(other.run_id)).status is RunStatus.SUCCEEDED
    calls.contexts.clear()
    with pytest.raises(DdakToolError, match="배포 기준이 바뀌었습니다"):
        if boundary == "approve":
            service.approve(stale.run_id, approver="fixture")
        else:
            service.start(stale.run_id)
    assert not calls.contexts
    assert service.store.project_state("demo")["lock"] is None


@pytest.mark.anyio
async def test_review_rechecks_baseline_after_acquiring_lock(rig, monkeypatch):
    service, source, calls = rig
    previous = support.seed_releases(service)
    planned = support.plan("review-lock-gap")
    service.prepare(
        planned,
        RunContext(
            planned.run_id,
            project="demo",
            review_baseline_hash=digest_json(service.store.environments("demo")),
        ),
        source,
    )
    service.approve(planned.run_id, approver="fixture")
    acquire = service.store.acquire

    def changed_before_acquire(*args, **kwargs):
        service.store.create_run("gap-release", "demo", digest_bytes(b"gap"))
        service.store.finish(
            "gap-release",
            "SUCCEEDED",
            {},
            {},
            {
                target: ("SUCCEEDED", {**release, "release_id": "gap-release"})
                for target, release in previous.items()
            },
        )
        return acquire(*args, **kwargs)

    monkeypatch.setattr(service.store, "acquire", changed_before_acquire)
    service.start(planned.run_id)
    assert (await service.wait(planned.run_id)).status is RunStatus.FAILED_BEFORE_DEPLOY
    assert not calls.contexts
    assert service.store.project_state("demo")["lock"] is None


def test_review_baseline_extension_preserves_legacy_context_encoding():
    context = RunContext("legacy", project="demo")
    assert "review_baseline_hash" not in context.to_json_dict()
    assert RunContext(**context.to_json_dict()).review_baseline_hash is None
    with pytest.raises(ValueError, match="재검토 배포 기준 해시"):
        RunContext("invalid", review_baseline_hash="invalid")


@pytest.mark.parametrize("phase", ["propose", "revise", "compose"])
def test_patch_loss_verdict_is_visible_and_blocks_all_approval_routes(registered_review, phase):
    harness = registered_review
    prepare_review_parent(harness)
    harness.corrupt[phase] = {
        "status": "patch_lost",
        "passed": False,
        "patch": None,
        "patch_sha256": None,
        "meta": None,
        "proposals": [],
        "violations": [PatchViolation(code="patch_lost", file="app.py", line=2)],
    }
    service, reviews = harness.service, harness.service.patch_reviews
    with client_for(service) as client:
        draft = begin(client, reviews)
        if phase == "compose":
            draft = finalize(client, reviews, draft)
        elif phase == "revise":
            identity = draft["proposals"][0]["id"]
            assert (
                post(
                    client,
                    "/runs/review-parent/patch-review",
                    action="revise:" + identity,
                    revision=draft["revision"],
                    **{"apply_" + identity: "on", "prompt_" + identity: "기존 설정을 검토해줘"},
                ).status_code
                == 303
            )
            wait_review(client, reviews)
            draft = reviews.get("review-parent")
        assert draft["state"] == "patch_lost" and draft["successor"] is None
        assert draft["loss_locations"] == [{"file": "app.py", "line": 2}]
        for page in ("patch-review", "approval"):
            html = client.get("/runs/review-parent/" + page).text
            assert 'data-code="patch_lost"' in html
            assert "app.py" in html and "2행" in html and "지금은 승인할 수 없습니다" in html
            assert "SECRET_KEY = " not in html and '"dev"' not in html
            assert "disabled" in html
        for action in ("begin", "cancel", "finalize", "save", "keep", "adopt"):
            assert (
                post(
                    client,
                    "/runs/review-parent/patch-review",
                    action=action,
                    revision=draft["revision"],
                ).status_code
                == 409
            )
        service.store.recover_patch_reviews()
        assert reviews.get("review-parent")["state"] == "patch_lost"
        assert post(client, "/runs/review-parent/approval", decision="approved").status_code == 409
        with pytest.raises(DdakToolError):
            service.start("review-parent")
        assert service.get_approvals("review-parent") == []
        assert not harness.replans and not harness.infra and not harness.calls.contexts
        assert post(client, "/runs/review-parent/approval", decision="denied").status_code == 303
        assert service.get_run("review-parent")["status"] == "FAILED_BEFORE_DEPLOY"


@pytest.mark.parametrize("new_proposal_fails", [False, True])
def test_registered_review_with_prior_ledger_reaches_one_final_approval(
    registered_review, monkeypatch, new_proposal_fails
):
    harness = registered_review
    if new_proposal_fails:
        (harness.source / "cookie.py").write_text(COOKIE)

        def unavailable(request):
            harness.provider.requests.append(request)
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "fixture failure")

        monkeypatch.setattr(harness.provider, "complete", unavailable)
    prepare_review_parent(harness)
    service, reviews = harness.service, harness.service.patch_reviews
    directory = service.root / "runs" / "prior"
    directory.mkdir(parents=True)
    patch = file_diff(
        "app.py", ORIGINAL.encode(), b'import os\nSECRET_KEY = os.environ["SECRET_KEY"]\n'
    )
    entries = save_ledger(directory, harness.source, patch)
    environments = {
        "local": {
            "status": "SUCCEEDED",
            "current": {
                "release_id": "prior",
                "patch_ledger": entries,
            },
            "previous": None,
        }
    }
    monkeypatch.setattr(service.store, "environments", lambda project: environments)
    with client_for(service) as client:
        draft = begin(client, reviews)
        assert draft["state"] == "ready" and draft["proposals"][0]["required"], draft
        if new_proposal_fails:
            assert draft["warnings"] and "AI_UNAVAILABLE" in draft["warnings"][0]
            html = client.get("/runs/review-parent/patch-review").text
            assert "일부 수정 제안을 준비하지 못했습니다" in html
            assert "AI_UNAVAILABLE" in html
        warnings = draft["warnings"]
        draft = finalize(client, reviews, draft)
        assert draft["state"] == "complete", draft
        child = draft["successor"]
        prepared = service._load_prepared(child)
        assert prepared.patch == patch and prepared.snapshot.patch_sha256 == digest_bytes(patch)
        assert bool(harness.provider.requests) is new_proposal_fails
        assert prepared.context.preparation_warnings == warnings
        if new_proposal_fails:
            assert "AI_UNAVAILABLE" in client.get(f"/runs/{child}/approval").text
        service.approve(child, approver="fixture")
        approvals = service.get_approvals(child)
        assert {record.kind for record in approvals} == {"patch", "deploy"}
        assert len({record.approval_id for record in approvals}) == 1
        assert len({record.bound_to for record in approvals}) == 2
