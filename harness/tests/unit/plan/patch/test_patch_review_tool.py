"""등록 patch_config의 검토 경로. 실제 관문 + replay 응답 + fixture scanner만 사용한다."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from ddak.app import load_tools
from ddak.core.ai.providers.replay import ReplayProvider
from ddak.core.config import LLMBackend, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.patch_review import (
    CodeProposal,
    PatchReviewRequest,
    ProposalBatch,
    ReviewEdit,
    ReviewResult,
)
from ddak.core.contracts.plan_facts import Facts
from ddak.core.contracts.tools.patch_config import (
    ApprovedPatch,
    PatchConfigInput,
    PatchConfigOutput,
)
from ddak.core.patch_ledger import file_diff, save_ledger
from ddak.core.runtime import current_run_id, current_tool, tool_context
from ddak.core.snapshots import digest_bytes
from ddak.plan.patch import (
    PatchPreparation,
    PatchSession,
    combine_review,
    generate,
    patch_session,
    pipeline,
    proposal_diff,
)

CONFIG = 'SECRET_KEY = "dev"\nAPP_BASE_URL = "http://localhost:5000"\n'
COOKIE = "SESSION_COOKIE_SECURE = False\n"
INTENTS = [
    {"file": "config.py", "line": 1, "pattern_id": "secret_key", "key": "SECRET_KEY"},
    {"file": "config.py", "line": 2, "pattern_id": "local_address", "key": "APP_BASE_URL"},
]
COOKIE_INTENT = {
    "file": "cookie.py",
    "line": 1,
    "pattern_id": "cookie_secure",
    "key": "SESSION_COOKIE_SECURE",
}
MODEL_REASON = "배포마다 주소와 서명 키를 주입하도록 변경했다"


def reply(intents, reason=MODEL_REASON):
    return {"intents": intents, "reason": reason}


class SeededReplay(ReplayProvider):
    """미리 지정한 응답만 임시 저장소에서 실제 ReplayProvider로 읽는다."""

    def __init__(self, root):
        super().__init__(root)
        self.replies = []
        self.requests = []

    def complete(self, request):
        assert current_tool.get() == "patch_config"
        assert current_run_id.get() == "review-run"
        assert self.replies, "AI 호출이 예상되지 않았다"
        self.requests.append(request)
        self.save(request, self.replies.pop(0))
        return super().complete(request)


class ReviewHarness:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.source = root / "snap"
        self.source.mkdir()
        (self.source / "config.py").write_text(CONFIG)
        self.runs = root / "runs"
        self.provider = SeededReplay(root / "replay")
        self.settings = Settings(llm_backend=LLMBackend.REPLAY, ai_retries=0)
        self.ctx = RunContext("review-run", toggles={"code_patch": True})
        self.scans = []
        self.reject_scan = False
        self.facts = None
        real_call = generate.call_ai

        def call(**kwargs):
            assert kwargs["settings"] is self.settings
            return real_call(**{**kwargs, "provider": self.provider})

        def scanner(source, patch):
            self.scans.append(patch)
            if self.reject_scan:
                raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "fixture scan rejected")

        monkeypatch.setattr(generate, "call_ai", call)
        monkeypatch.setattr(pipeline, "strict_patch_scan", scanner)
        self.tool = load_tools().get("patch_config")

    def run(self, action="propose", *, previous=None, **kwargs):
        request = None if action is None else PatchReviewRequest(action=action, **kwargs)
        session = PatchSession(self.ctx.run_id, self.root, self.runs, self.facts, self.settings)
        with patch_session(session), tool_context("patch_config", self.ctx.run_id):
            return self.tool.fn(
                PatchConfigInput(
                    run_id=self.ctx.run_id, source_dir="snap", review=request, previous=previous
                ),
                self.ctx,
            )

    def propose(self, *, two_files=False):
        intents = list(INTENTS)
        if two_files:
            (self.source / "cookie.py").write_text(COOKIE)
            intents.append(COOKIE_INTENT)
        self.provider.replies.append(reply(intents))
        return self.run()

    def with_ledger(self):
        first = self.propose()
        (self.runs / "release-1").mkdir(parents=True)
        ledger = save_ledger(self.runs / "release-1", self.source, first.patch.encode())
        self.ctx = replace(
            self.ctx,
            previous_release={"local": {"release_id": "release-1", "patch_ledger": ledger}},
        )
        (self.source / "cookie.py").write_text(COOKIE)
        self.provider.replies.append(reply([COOKIE_INTENT]))
        return first, ledger, self.run()


@pytest.fixture
def review(tmp_path, monkeypatch):
    return ReviewHarness(tmp_path, monkeypatch)


def assert_ready(review, output):
    assert output.passed is True and output.patch and output.meta
    assert output.patch_sha256 == digest_bytes(output.patch.encode())
    assert PatchPreparation.from_output(output).patch == output.patch.encode()
    assert (
        combine_review(review.source, output.proposals, [p.id for p in output.proposals])
        == output.patch.encode()
    )
    assert PatchConfigOutput.model_validate_json(output.model_dump_json()) == output


def proposal_for(output, name):
    return next(p for p in output.proposals if p.edits[0].path == name)


def test_additive_contract_and_old_review_models():
    item = CodeProposal(
        id="config",
        title="설정",
        reason="이유",
        edits=[ReviewEdit(path="config.py", start=1, end=1, lines=["x = 1"])],
    )
    assert item.revision == 1 and item.required is False
    assert ProposalBatch(proposals=[item]).proposals == [item]
    assert ReviewResult([item], Source.REPLAY).source is Source.REPLAY
    assert PatchConfigInput(run_id="run", source_dir="snap").review is None
    assert PatchConfigOutput(run_id="run", status="no_targets", passed=False).proposals == []
    with pytest.raises(ValidationError):
        PatchReviewRequest(action="revise", prompt="가" * 2001)
    with pytest.raises(ValidationError):
        CodeProposal.model_validate({**item.model_dump(), "required": "false"})


def test_propose_uses_registry_groups_entire_files_and_preserves_source(review):
    output = review.propose(two_files=True)
    assert_ready(review, output)
    assert review.tool.fn.__module__ == "ddak.plan.patch.tool"
    assert output.status == "proposed" and output.attempts == 1
    assert output.source is Source.REPLAY and output.meta.source is Source.REPLAY
    assert len(output.proposals) == 2
    assert all(not p.required and p.reason == MODEL_REASON for p in output.proposals)
    for proposal in output.proposals:
        assert len({e.path for e in proposal.edits}) == 1
        assert "import os" in proposal_diff(review.source, proposal, output.proposals)
    assert proposal_for(output, "config.py").env_vars == ["APP_BASE_URL", "SECRET_KEY"]
    assert {k.name for k in output.env_keys} == set(output.env_vars)
    assert output.changed_files == ["config.py", "cookie.py"]
    assert review.scans == [output.patch.encode()]
    sent = review.provider.requests[0].user
    assert "localhost" not in sent and CONFIG not in sent and "dev" not in sent
    assert (review.source / "config.py").read_text() == CONFIG
    assert (review.source / "cookie.py").read_text() == COOKIE


def test_nonreview_registered_flow_preserves_fields_and_empty_proposals(review):
    review.provider.replies.append(reply(INTENTS))
    output = review.run(None)
    assert output.passed and output.proposals == []
    assert output.attempts == 1 and output.targets == {"config.py": ["local_address", "secret_key"]}
    assert PatchPreparation.from_output(output).patch == output.patch.encode()


def test_revision_scopes_intents_preserves_id_and_uses_masked_model_reason(review):
    initial = review.propose(two_files=True)
    previous = proposal_for(initial, "config.py")
    credential = "fixture-" + "provider-credential"
    review.settings = replace(review.settings, llm_api_key=credential)
    reason = "검토 결과: " + credential
    review.provider.replies.append(reply(INTENTS, reason))
    output = review.run(
        "revise",
        proposals=initial.proposals,
        proposal_id=previous.id,
        prompt="이 파일의 기존 설정만 검토해줘 " + credential,
    )
    assert_ready(review, output)
    revised = proposal_for(output, "config.py")
    assert revised.id == previous.id and revised.revision == previous.revision + 1
    assert revised.requires == previous.requires
    assert revised.reason == "검토 결과: [REDACTED]"
    assert proposal_for(output, "cookie.py") == proposal_for(initial, "cookie.py")
    sent = review.provider.requests[-1].user
    assert "운영자 요청: 이 파일의 기존 설정만 검토해줘" in sent
    assert credential not in sent and "[REDACTED]" in sent
    assert "cookie.py" not in sent and "localhost" not in sent and "import os" not in sent
    assert output.attempts == 1 and len(review.provider.requests) == 2


@pytest.mark.parametrize("bad", [[], [COOKIE_INTENT]])
def test_revision_rejects_empty_or_expanded_scope_without_replacing_old_proposal(review, bad):
    initial = review.propose(two_files=True)
    previous = proposal_for(initial, "config.py")
    review.provider.replies.extend([reply(bad), reply(bad)])
    with pytest.raises(DdakToolError):
        review.run("revise", proposals=initial.proposals, proposal_id=previous.id)
    assert previous.revision == 1 and previous.reason == MODEL_REASON
    assert (review.source / "config.py").read_text() == CONFIG


def test_compose_is_ai_free_and_matches_exact_selected_diff(review):
    initial = review.propose(two_files=True)
    chosen = proposal_for(initial, "cookie.py")
    calls = len(review.provider.requests)
    output = review.run("compose", proposals=initial.proposals, selected=[chosen.id])
    expected = combine_review(review.source, initial.proposals, [chosen.id])
    assert output.patch.encode() == expected
    assert output.changed_files == ["cookie.py"]
    assert output.env_vars == ["SESSION_COOKIE_SECURE"]
    assert output.attempts == 0 and output.source is None and output.meta.source is Source.CACHE
    assert output.status == "proposed" and not output.meta.reuse
    assert len(review.provider.requests) == calls and review.scans[-1] == expected
    assert PatchPreparation.from_output(output).patch == expected


def test_empty_selection_is_explicit_no_targets_without_ai(review):
    initial = review.propose()
    output = review.run("compose", proposals=initial.proposals, selected=[])
    assert output.status == "no_targets" and not output.passed and output.patch is None
    assert output.warnings == [] and output.attempts == 0
    assert len(review.provider.requests) == 1


@pytest.mark.parametrize("change", ["unknown", "duplicate", "dependency", "cycle", "missing", "id"])
def test_invalid_selection_and_dependency_graph_raise_instead_of_fallback(review, change):
    initial = review.propose(two_files=True)
    a, b = initial.proposals
    selected = [a.id, b.id]
    if change == "unknown":
        selected = ["unknown"]
    elif change == "duplicate":
        selected = [a.id, a.id]
    elif change == "dependency":
        a.requires = [b.id]
        selected = [a.id]
    elif change == "cycle":
        a.requires, b.requires = [b.id], [a.id]
    elif change == "missing":
        a.requires = ["unknown"]
    else:
        b.id = a.id
    with pytest.raises(DdakToolError):
        review.run("compose", proposals=[a, b], selected=selected)
    assert len(review.provider.requests) == 1


def test_valid_dependency_and_preview(review):
    initial = review.propose(two_files=True)
    a, b = initial.proposals
    a.requires = [b.id]
    output = review.run("compose", proposals=[a, b], selected=[b.id, a.id])
    displayed = proposal_diff(review.source, a, [a, b])
    assert '"dev"' not in displayed and "[REDACTED]" in displayed
    assert "+import os" in displayed and "b/cookie.py" in displayed
    assert combine_review(review.source, [a, b], [b.id, a.id]).decode() == output.patch
    assert output.attempts == 0


@pytest.mark.parametrize("tamper", ["env", "split", "path", "code"])
def test_untrusted_proposals_cannot_bypass_render_and_check(review, tamper):
    initial = review.propose()
    proposal = initial.proposals[0]
    if tamper == "env":
        proposal.env_vars = []
    elif tamper == "split":
        other = proposal.model_copy(deep=True, update={"id": "split"})
        initial.proposals.append(other)
    elif tamper == "path":
        proposal.edits[0].path = "../outside.py"
    else:
        proposal.edits[0].lines.append("print('unexpected')")
    with pytest.raises(DdakToolError):
        review.run(
            "compose", proposals=initial.proposals, selected=[p.id for p in initial.proposals]
        )
    assert len(review.provider.requests) == 1


def test_ledger_reuse_is_required_and_composition_preserves_original_entry(review):
    first, ledger, initial = review.with_ledger()
    assert_ready(review, initial)
    required = proposal_for(initial, "config.py")
    assert required.required and not proposal_for(initial, "cookie.py").required
    assert initial.attempts == 1 and initial.changed_files == ["config.py", "cookie.py"]
    assert "config.py" not in review.provider.requests[-1].user
    output = review.run(
        "compose", proposals=initial.proposals, selected=[p.id for p in initial.proposals]
    )
    assert_ready(review, output)
    (review.runs / "release-2").mkdir()
    next_ledger = save_ledger(review.runs / "release-2", review.source, output.patch.encode())
    assert next_ledger["config.py"] == ledger["config.py"]
    reused = review.run("compose", proposals=initial.proposals, selected=[required.id])
    assert reused.patch == first.patch and reused.status == "reused" and reused.meta.reuse
    assert reused.source is None and reused.meta.source is Source.CACHE and reused.attempts == 0
    assert len(review.provider.requests) == 2
    assert proposal_diff(review.source, proposal_for(initial, "cookie.py"), initial.proposals)


@pytest.mark.parametrize("action", ["omit", "unmark", "remove", "mutate", "revise"])
def test_required_ledger_proposal_cannot_be_omitted_mutated_or_revised(review, action):
    _, _, initial = review.with_ledger()
    required = proposal_for(initial, "config.py")
    chosen = [p.id for p in initial.proposals]
    if action == "omit":
        chosen.remove(required.id)
    elif action == "unmark":
        required.required = False
    elif action == "remove":
        initial.proposals.remove(required)
        chosen.remove(required.id)
    elif action == "mutate":
        required.edits[0].lines = [
            line.replace("'SECRET_KEY'", '"SECRET_KEY"') for line in required.edits[0].lines
        ]
    with pytest.raises(DdakToolError):
        review.run(
            "revise" if action == "revise" else "compose",
            proposals=initial.proposals,
            selected=chosen,
            proposal_id=required.id,
        )
    assert len(review.provider.requests) == 2


def test_approved_patch_compatibility_and_toggle_off_reuse(review):
    first = review.propose()
    review.ctx = replace(review.ctx, toggles={"code_patch": False})
    approved = ApprovedPatch(patch=first.patch, reason="이전 승인", source=Source.REPLAY)
    initial = review.run(previous=approved)
    assert initial.status == "reused" and all(p.required for p in initial.proposals)
    output = review.run(
        "compose",
        previous=approved,
        proposals=initial.proposals,
        selected=[p.id for p in initial.proposals],
    )
    assert_ready(review, output)
    assert output.meta.source is Source.CACHE and output.source is None and output.attempts == 0
    assert len(review.provider.requests) == 1


def test_compose_scanner_failure_cannot_silently_become_no_targets(review):
    initial = review.propose()
    review.reject_scan = True
    with pytest.raises(DdakToolError, match="선택된 패치와 검사 결과"):
        review.run(
            "compose", proposals=initial.proposals, selected=[p.id for p in initial.proposals]
        )
    assert len(review.scans) == 2 and len(review.provider.requests) == 1


def test_compose_scanner_failure_cannot_silently_keep_only_reused_files(review, monkeypatch):
    _, _, initial = review.with_ledger()
    scans = []

    def scanner(source, patch):
        scans.append(patch)
        if b"cookie.py" in patch:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "fixture rejection")

    monkeypatch.setattr(pipeline, "strict_patch_scan", scanner)
    with pytest.raises(DdakToolError, match="선택된 패치와 검사 결과"):
        review.run(
            "compose", proposals=initial.proposals, selected=[p.id for p in initial.proposals]
        )
    assert len(scans) == 2 and b"cookie.py" not in scans[-1]
    assert len(review.provider.requests) == 2


def test_compose_uses_loss_guard_for_changed_ledger_source(review):
    _, _, initial = review.with_ledger()
    (review.source / "config.py").write_text(CONFIG + "VERSION = 2\n")
    proposal_for(initial, "config.py").required = False
    chosen = proposal_for(initial, "cookie.py")
    output = review.run("compose", proposals=initial.proposals, selected=[chosen.id])
    assert output.status == "patch_lost" and output.passed is False
    assert output.patch is None and output.meta is None and output.proposals == []
    assert {v.file for v in output.violations} == {"config.py"}
    with pytest.raises(DdakToolError, match="손실"):
        PatchPreparation.from_output(output)
    assert len(review.provider.requests) == 2


def test_revision_replaces_rendered_intent_key_in_same_allowed_target(review):
    (review.source / "config.py").write_text("connect('localhost')\n")
    intent = {"file": "config.py", "line": 1, "pattern_id": "local_address", "key": "APP_BASE_URL"}
    review.provider.replies.append(reply([intent]))
    initial = review.run()
    previous = initial.proposals[0]
    review.provider.replies.append(reply([{**intent, "key": "DATABASE_URL"}], "DB 주소 주입"))
    output = review.run(
        "revise", proposals=initial.proposals, proposal_id=previous.id, prompt="DB 접속 주소야"
    )
    assert_ready(review, output)
    revised = output.proposals[0]
    assert revised.id == previous.id and revised.revision == 2
    assert revised.reason == "DB 주소 주입" and revised.env_vars == ["DATABASE_URL"]
    assert "os.environ['DATABASE_URL']" in output.patch
    assert output.patch != initial.patch and output.patch_sha256 != initial.patch_sha256
    assert output.env_vars == ["DATABASE_URL"]
    assert {k.name for k in output.env_keys} == {"DATABASE_URL"}


def test_compose_outside_session_targets_cannot_silently_become_no_targets(review):
    initial = review.propose()
    review.facts = Facts(
        project="demo",
        mode=review.ctx.mode,
        target="local",
        tiers=("was",),
        changed={"local": {"was": True}},
        code_patch=True,
        patch_targets=(),
        source_snapshot_hash=digest_bytes(b"source"),
        facts_hash=digest_bytes(b"facts"),
    )
    with pytest.raises(DdakToolError, match="선택된 패치와 검사 결과"):
        review.run(
            "compose", proposals=initial.proposals, selected=[p.id for p in initial.proposals]
        )
    assert len(review.provider.requests) == 1


def test_changed_ledger_source_cannot_drop_old_patch_when_toggle_off(review):
    review.with_ledger()
    (review.source / "config.py").write_text(CONFIG + "VERSION = 2\n")
    review.ctx = replace(review.ctx, toggles={"code_patch": False})
    output = review.run()
    assert output.status == "patch_lost" and output.passed is False
    with pytest.raises(DdakToolError, match="손실"):
        PatchPreparation.from_output(output)


@pytest.mark.parametrize("enabled", [False, True])
def test_mixed_legacy_optional_review_reuses_valid_files_and_rechecks_only_invalid(review, enabled):
    first = review.propose()
    (review.source / "cookie.py").write_text(COOKIE)
    legacy = file_diff(
        "cookie.py",
        COOKIE.encode(),
        b"import os\nSESSION_COOKIE_SECURE = "
        b'os.getenv("SESSION_COOKIE_SECURE", "false") == "true"\n',
    )
    directory = review.runs / "legacy"
    directory.mkdir(parents=True)
    entries = save_ledger(directory, review.source, first.patch.encode() + legacy)
    review.ctx = replace(
        review.ctx,
        toggles={"code_patch": enabled},
        previous_release={"local": {"release_id": "legacy", "patch_ledger": entries}},
    )
    if enabled:
        review.provider.replies.append(reply([COOKIE_INTENT]))
    output = review.run()
    if enabled:
        assert_ready(review, output)
        assert proposal_for(output, "config.py").required
        assert not proposal_for(output, "cookie.py").required
        assert "config.py" not in review.provider.requests[-1].user
        assert "os.getenv" not in output.patch and len(review.provider.requests) == 2
        composed = review.run(
            "compose", proposals=output.proposals, selected=[p.id for p in output.proposals]
        )
        assert composed.patch == output.patch and composed.attempts == 0
    else:
        assert output.status == "patch_lost" and output.passed is False
        assert output.patch is None and output.patch_sha256 is None and output.meta is None
        assert [(v.file, v.line) for v in output.violations] == [("cookie.py", 1)]
        assert output.proposals == [] and len(review.provider.requests) == 1


@pytest.mark.parametrize("ending", ["\n", "\r\n", ""])
def test_compose_bytes_are_canonical_for_source_line_endings(review, ending):
    text = "SECRET_KEY = 'dev'" + ending
    (review.source / "config.py").write_bytes(text.encode())
    review.provider.replies.append(reply(INTENTS[:1]))
    initial = review.run()
    assert_ready(review, initial)
    output = review.run(
        "compose", proposals=initial.proposals, selected=[p.id for p in initial.proposals]
    )
    assert output.patch == initial.patch
    assert output.patch_sha256 == initial.patch_sha256
    assert (review.source / "config.py").read_bytes() == text.encode()


def test_review_call_still_requires_registered_tool_context(review):
    with tool_context("health_check", review.ctx.run_id), pytest.raises(DdakToolError) as error:
        generate.patch_config(
            PatchConfigInput(
                run_id=review.ctx.run_id,
                source_dir="snap",
                review=PatchReviewRequest(action="propose"),
            ),
            review.ctx,
            root=review.root,
            settings=review.settings,
        )
    assert error.value.code is ErrorCode.AI_NOT_ALLOWED
    assert review.provider.requests == []


def test_propose_failure_retains_existing_rejected_contract(review):
    review.provider.replies.extend([reply([COOKIE_INTENT]), reply([COOKIE_INTENT])])
    output = review.run()
    assert output.status == "rejected" and not output.passed
    assert output.proposals == [] and output.patch is None and output.patch_sha256 is None
    assert output.meta is None and output.violations and output.warnings
    assert output.attempts == 2
