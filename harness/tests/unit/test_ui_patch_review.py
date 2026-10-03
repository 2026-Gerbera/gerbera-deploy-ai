"""실제 HTTP·저장소·선택 패치 binding. AI와 배포 도구는 fixture만 사용한다."""

import asyncio
from dataclasses import replace

import pytest

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.patch_review import CodeProposal, ReviewResult
from ddak.core.snapshots import digest_bytes, materialize
from ddak.executor.patch_review import PatchReviews
from ddak.plan.patch import combine_review, proposal_diff
from tests.unit import test_deployment_service as support
from tests.unit.test_ui_integration_fix10 import BASE, client_for

rig = support.rig
ORIGINAL = 'import os\nSECRET_KEY = "dev"\n'
COOKIE = "import os\nSESSION_COOKIE_SECURE = False\n"


def post(client, path, **data):
    return client.post(
        path,
        data={"csrf_token": client.cookies.get("ddak_csrf"), **data},
        headers={"origin": BASE, "accept": "text/html", "X-Ddak-Form": "1"},
        follow_redirects=False,
    )


def proposal(identity, line, code, env, path="app.py"):
    return CodeProposal(
        id=identity,
        title=identity + " 설정 수정",
        reason="환경변수로 설정",
        edits=[{"path": path, "start": line, "end": line, "lines": [code]}],
        env_vars=[env],
    )


@pytest.fixture
def review_rig(rig):
    service, source, calls = rig
    (source / "app.py").write_text(ORIGINAL)
    (source / "cookie.py").write_text(COOKIE)
    plan = support.plan("review-parent")
    context = RunContext(
        plan.run_id, project=plan.project, repo_url="https://github.com/example/app.git"
    )
    service.prepare(plan, context, source)
    items = [
        proposal("secret", 2, 'SECRET_KEY = os.environ["SECRET_KEY"]', "SECRET_KEY"),
        proposal(
            "cookie",
            2,
            'SESSION_COOKIE_SECURE = os.environ["SESSION_COOKIE_SECURE"] == "true"',
            "SESSION_COOKIE_SECURE",
            path="cookie.py",
        ),
    ]
    seen = []

    def generate(source, context, *, previous=None, allproposals=None, prompt=""):
        assert context.toggles["code_patch"]
        seen.append((previous.id if previous else None, prompt))
        if prompt == "실패":
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "검토 연결 실패")
        if previous:
            return ReviewResult(
                [previous.model_copy(update={"reason": "추가 검토 완료"})], Source.REPLAY
            )
        return ReviewResult(items, Source.REPLAY)

    async def finalize(prepared, patch, draft, guard):
        guard()
        plan = support.plan("review-child", patch=bool(patch))
        context = replace(prepared.context, run_id=plan.run_id, toggles=plan.toggles)
        return service.prepare(
            plan,
            context,
            source,
            patch=patch,
            patch_meta=support.patch_metadata(patch) if patch else None,
            review_parent=(prepared.plan.run_id, draft["revision"]),
        )

    reviews = PatchReviews(
        service, generate=generate, combine=combine_review, finalize=finalize, diff=proposal_diff
    )
    service.patch_reviews = reviews
    return service, source, calls, reviews, seen


def wait_review(client, reviews, run_id="review-parent"):
    async def wait():
        await reviews.tasks[run_id]

    client.portal.call(wait)


def begin(client, reviews):
    client.get("/runs/review-parent/patch-review")
    assert post(client, "/runs/review-parent/patch-review", action="begin").status_code == 303
    wait_review(client, reviews)
    return reviews.get("review-parent")


def test_partial_selection_reprepares_immutable_approval_and_exact_build(review_rig, tmp_path):
    service, source, calls, reviews, _ = review_rig
    with client_for(service) as client:
        initial = service.store.prepared("review-parent")
        draft = begin(client, reviews)
        html = client.get("/runs/review-parent/patch-review").text
        assert "2개 중 2개 선택" in html
        assert 'name="prompt_cookie"' in html and 'name="apply_secret"' in html
        assert post(client, "/runs/review-parent/approval", decision="approved").status_code == 409
        response = post(
            client,
            "/runs/review-parent/patch-review",
            action="finalize",
            revision=draft["revision"],
            apply_cookie="on",
        )
        assert response.status_code == 303
        wait_review(client, reviews)
        assert service.store.prepared("review-parent") == initial
        assert service.get_run("review-parent")["status"] == "SUPERSEDED"
        child = service._load_prepared("review-child")
        assert child.snapshot.patch_sha256 == digest_bytes(child.patch)
        materialize(source, tmp_path / "built", child.snapshot, child.patch)
        built = (tmp_path / "built/app.py").read_text()
        assert 'SECRET_KEY = "dev"' in built
        assert "SESSION_COOKIE_SECURE = os.environ[" in (tmp_path / "built/cookie.py").read_text()
        assert not calls.contexts
        assert (
            client.get("/runs/review-parent/patch-review", follow_redirects=False).headers[
                "location"
            ]
            == "/runs/review-child/approval"
        )
        approved = client.get("/runs/review-child/approval").text
        assert "선택한 코드 수정 1개" in approved
        assert post(client, "/runs/review-parent/approval", decision="approved").status_code == 409
        response = post(client, "/runs/review-child/approval", decision="approved")
        assert response.status_code == 303
        client.portal.call(service.wait, "review-child")
        assert service.get_run("review-child")["status"] == "SUCCEEDED"
        assert (service.root / "runs/review-child/build-source/app.py").read_text() == built
        assert any(
            record.bound_to == child.snapshot.patch_sha256
            for record in service.get_approvals("review-child")
        )


@pytest.mark.parametrize("decision", ["adopt", "keep"])
def test_prompt_result_requires_explicit_adoption_and_revision(review_rig, decision):
    _, source, _, reviews, seen = review_rig
    with client_for(review_rig[0]) as client:
        draft = begin(client, reviews)
        response = post(
            client,
            "/runs/review-parent/patch-review",
            action="revise:cookie",
            revision=draft["revision"],
            apply_cookie="on",
            prompt_cookie="HTTPS 기본값도 검토해줘",
            prompt_secret="이 항목은 다음에 검토해줘",
        )
        assert response.status_code == 303
        wait_review(client, reviews)
        current = reviews.get("review-parent")
        assert seen[-1] == ("cookie", "HTTPS 기본값도 검토해줘")
        assert current["notes"]["secret"] == "이 항목은 다음에 검토해줘"
        assert current["proposals"][1]["revision"] == 1
        assert current["candidate"]["proposal"]["revision"] == 2
        html = client.get("/runs/review-parent/patch-review").text
        assert "새 제안 채택" in html and "현재안 유지" in html
        assert "이 항목은 다음에 검토해줘</textarea>" in html
        assert (
            post(
                client,
                "/runs/review-parent/patch-review",
                action=decision,
                revision=draft["revision"],
                candidate_id=current["candidate"]["id"],
            ).status_code
            == 409
        )
        assert (
            post(
                client,
                "/runs/review-parent/patch-review",
                action=decision,
                revision=current["revision"],
                candidate_id="wrong",
            ).status_code
            == 409
        )
        assert (
            post(
                client,
                "/runs/review-parent/patch-review",
                action=decision,
                revision=current["revision"],
                candidate_id=current["candidate"]["id"],
                apply_cookie="on",
            ).status_code
            == 303
        )
        saved = reviews.get("review-parent")
        assert saved["candidate"] is None
        assert saved["proposals"][1]["revision"] == (2 if decision == "adopt" else 1)
        assert (source / "app.py").read_text() == ORIGINAL


def test_failed_revision_preserves_proposals_and_selection(review_rig):
    service, _, _, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        post(
            client,
            "/runs/review-parent/patch-review",
            action="revise:cookie",
            revision=draft["revision"],
            apply_secret="on",
            prompt_cookie="실패",
        )
        wait_review(client, reviews)
        current = reviews.get("review-parent")
        assert current["proposals"] == draft["proposals"] and current["selected"] == ["secret"]
        assert current["state"] == "ready" and current["error"] == "검토 연결 실패"


def test_empty_selection_and_cancel_do_not_approve(review_rig):
    service, _, calls, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        post(
            client, "/runs/review-parent/patch-review", action="cancel", revision=draft["revision"]
        )
        assert service.get_approvals("review-parent") == []
        draft = begin(client, reviews)
        post(
            client,
            "/runs/review-parent/patch-review",
            action="finalize",
            revision=draft["revision"],
        )
        wait_review(client, reviews)
        assert service._load_prepared("review-child").patch is None
        assert service.get_approvals("review-child") == [] and not calls.contexts


def test_csrf_busy_unknown_selection_and_source_change(review_rig):
    service, source, _, reviews, _ = review_rig
    with client_for(service) as client:
        assert (
            client.post("/runs/review-parent/patch-review", data={"action": "begin"}).status_code
            == 403
        )
        draft = begin(client, reviews)
        assert (
            post(
                client,
                "/runs/review-parent/patch-review",
                action="finalize",
                revision=draft["revision"],
                apply_unknown="on",
            ).status_code
            == 409
        )
        current = service.store.save_patch_review(
            "review-parent", draft["revision"], {**draft, "state": "reviewing"}
        )
        assert (
            post(
                client,
                "/runs/review-parent/patch-review",
                action="cancel",
                revision=current["revision"],
            ).status_code
            == 409
        )
        service.store.recover_patch_reviews()
        recovered = reviews.get("review-parent")
        assert recovered["revision"] > current["revision"] and recovered["state"] == "ready"
        assert recovered["proposals"] == draft["proposals"]
        (source / "app.py").write_text(ORIGINAL + "# source changed\n")
        failed = post(
            client,
            "/runs/review-parent/patch-review",
            action="finalize",
            revision=recovered["revision"],
        )
        assert failed.status_code == 409
        assert failed.json()["error"]["code"] == "PRECONDITION_FAILED"
        blocked = client.get("/runs/review-parent/patch-review").text
        assert "승인 자료를 다시 준비해야 합니다" in blocked
        assert "선택한 수정으로 승인 자료 준비</button>" not in blocked
        with pytest.raises(DdakToolError, match="원본 코드"):
            reviews.request("review-parent", recovered["revision"], "finalize", [])


def test_background_request_does_not_replace_newer_revision(review_rig):
    service, _, _, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        saved = service.store.save_patch_review(
            "review-parent", draft["revision"], {**draft, "selected": []}
        )
        reviews._failed("review-parent", draft, "old error")
        assert reviews.get("review-parent") == saved


def test_async_job_indicates_progress_and_cannot_be_double_submitted(review_rig):
    service, _, _, reviews, _ = review_rig
    gate = asyncio.Event()
    original = reviews.finalize

    async def delayed(*args):
        await gate.wait()
        return await original(*args)

    reviews.finalize = delayed
    with client_for(service) as client:
        draft = begin(client, reviews)
        post(
            client,
            "/runs/review-parent/patch-review",
            action="finalize",
            revision=draft["revision"],
            apply_cookie="on",
        )
        html = client.get("/runs/review-parent/patch-review").text
        assert "data-live-region" in html and 'class="state running"' in html
        assert 'http-equiv="refresh"' not in html
        assert post(client, "/runs/review-parent/approval", decision="approved").status_code == 409
        current = reviews.get("review-parent")
        assert (
            post(
                client,
                "/runs/review-parent/patch-review",
                action="finalize",
                revision=current["revision"],
            ).status_code
            == 409
        )
        client.portal.call(gate.set)
        wait_review(client, reviews)


def test_required_previous_patch_cannot_be_unchecked_or_revised(review_rig):
    service, _, _, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        draft["proposals"][0]["required"] = True
        current = service.store.save_patch_review("review-parent", draft["revision"], draft)
        page = client.get("/runs/review-parent/patch-review").text
        assert 'type="hidden" name="apply_secret"' in page
        assert 'name="prompt_secret"' not in page
        for action, selected in (("finalize", []), ("revise:secret", ["secret"])):
            with pytest.raises(DdakToolError, match="이전 승인 수정"):
                reviews.request(
                    "review-parent", current["revision"], action, selected, prompt="변경"
                )
        assert service.get_approvals("review-parent") == []


@pytest.mark.parametrize("recovery", ["failure", "restart"])
def test_unpublished_child_is_never_approvable_and_is_cancelled(review_rig, monkeypatch, recovery):
    service, source, _, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        if recovery == "failure":

            def fail_publish(parent, revision, child):
                assert reviews.get(child)["state"] == "publishing"
                with pytest.raises(DdakToolError):
                    service.approve(child, approver="fixture")
                raise RuntimeError("fixture publish failure")

            monkeypatch.setattr(service.store, "publish_patch_review", fail_publish)
            post(
                client,
                "/runs/review-parent/patch-review",
                action="finalize",
                revision=draft["revision"],
                apply_secret="on",
                apply_cookie="on",
            )
            wait_review(client, reviews)
        else:
            current = service.store.save_patch_review(
                "review-parent", draft["revision"], {**draft, "state": "finalizing"}
            )
            plan = support.plan("review-child")
            service.prepare(
                plan,
                RunContext(plan.run_id, project="demo"),
                source,
                review_parent=("review-parent", current["revision"]),
            )
            with pytest.raises(DdakToolError):
                service.approve("review-child", approver="fixture")
            service.store.recover_patch_reviews()
        assert service.get_run("review-child")["status"] == "CANCELLED"
        assert reviews.get("review-child")["state"] == "cancelled"
        assert reviews.get("review-parent")["state"] == "ready"
        assert service.get_run("review-parent")["status"] == "AWAITING_APPROVAL"
        assert service.get_approvals("review-child") == []
        with pytest.raises(DdakToolError):
            service.approve("review-child", approver="fixture")


def test_current_and_candidate_display_masks_secrets_without_changing_patch(review_rig):
    service, _, _, reviews, _ = review_rig
    raw = 'SECRET_KEY = "fixture-sensitive-removed-value"\n'
    with client_for(service) as client:
        draft = begin(client, reviews)
        candidate = {
            "id": "fixture",
            "source": "replay",
            "base_revision": draft["revision"],
            "proposal": draft["proposals"][0],
        }
        service.store.save_patch_review(
            "review-parent", draft["revision"], {**draft, "candidate": candidate}
        )
        before = service.store.patch_review("review-parent")
        reviews.diff = lambda *args: raw
        view = reviews.view("review-parent")
        assert "fixture-sensitive-removed-value" not in view["items"][0]["diff"]
        assert "fixture-sensitive-removed-value" not in view["candidate"]["diff"]
        assert (
            "fixture-sensitive-removed-value"
            not in client.get("/runs/review-parent/patch-review").text
        )
        assert service.store.patch_review("review-parent") == before


@pytest.mark.parametrize(
    "original",
    [
        'SECRET_KEY: str = "fixture-private-typed"\n',
        'SECRET_KEY: str = ("fixture-private-" "typed")\n',
    ],
)
def test_real_display_diff_masks_typed_or_joined_secret(review_rig, original):
    from ddak.core.patch_patterns import scan_patch_targets
    from ddak.plan.patch.intents import EditIntent, render_intents
    from ddak.plan.patch.review import file_proposals

    service, source, _, reviews, _ = review_rig
    with client_for(service) as client:
        draft = begin(client, reviews)
        # The fixture below is a distinct source/display exercise; approval data is unchanged.
        display_source = source.parent / "display-source"
        display_source.mkdir()
        (display_source / "config.py").write_text(original)
        targets = scan_patch_targets(display_source, ())
        intents = [
            EditIntent(file=t.file, line=t.line, pattern_id=t.pattern_id, key=t.key)
            for t in targets
            if t.severity == "patch"
        ]
        patch, _ = render_intents(display_source, targets, intents)
        proposals = file_proposals(display_source, patch, required_files=set(), reason="설정 분리")
        candidate = {
            "id": "redact-check",
            "source": "fixture",
            "base_revision": draft["revision"],
            "proposal": proposals[0].model_dump(mode="json"),
        }
        service.store.save_patch_review(
            "review-parent",
            draft["revision"],
            {
                **draft,
                "proposals": [p.model_dump(mode="json") for p in proposals],
                "candidate": candidate,
            },
        )
        reviews.diff = lambda _, item, allitems: proposal_diff(display_source, item, allitems)
        before = digest_bytes(patch)
        view = reviews.view("review-parent")
        assert "fixture-private-typed" not in view["items"][0]["diff"]
        assert "fixture-private-typed" not in view["candidate"]["diff"]
        assert "[REDACTED]" in view["items"][0]["diff"]
        assert "fixture-private-typed" not in client.get("/runs/review-parent/patch-review").text
        assert (
            digest_bytes(combine_review(display_source, proposals, [p.id for p in proposals]))
            == before
        )


def test_display_redactor_masks_multiline_and_preserves_environment_key_name():
    from ddak.core.redact import redact_python

    text = 'SECRET_KEY: str = """first-private-line\nsecond-private-line"""\n'
    masked = redact_python(text)
    assert "first-private-line" not in masked and "second-private-line" not in masked
    assert "[REDACTED]" in masked
    safe = 'SECRET_KEY = os.environ["SECRET_KEY"]\n'
    assert redact_python(safe) == safe
    assert "contents" not in redact_python('SECRET_KEY = "contents')
