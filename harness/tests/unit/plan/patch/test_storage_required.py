"""필수 저장 경로 보완은 가짜 응답·스캐너로만 검증한다."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ddak.app import load_tools
from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.config import LLMBackend, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.patch_review import PatchReviewRequest
from ddak.core.contracts.plan_facts import Facts
from ddak.core.contracts.tools.patch_config import (
    ApprovedPatch,
    PatchConfigInput,
    PatchConfigOutput,
)
from ddak.core.patch_patterns import scan_patch_targets
from ddak.core.runtime import tool_context
from ddak.core.snapshots import apply_diff, copy_source, digest_bytes
from ddak.core.storage import scan_storage
from ddak.executor.approval_meta import encode_meta
from ddak.plan.patch import generate, pipeline
from ddak.plan.patch.intents import EditIntent, render_intents
from ddak.plan.patch.pipeline import PatchPreparation, PatchSession, patch_session, prepare_patch

SETTINGS = Settings(ai_retries=0, llm_backend=LLMBackend.API, llm_model="fixture-model")
MARKER = "fixture-" + "storage-path-" + "not-for-provider"
ORIGINAL = f"IMG_DIR = '{MARKER}'\n"
STORAGE_INTENT = {
    "file": "config.py",
    "line": 1,
    "pattern_id": "local_storage_dir",
    "key": "IMG_DIR",
}
SECRET_INTENT = {
    "file": "secret.py",
    "line": 1,
    "pattern_id": "secret_key",
    "key": "SECRET_KEY",
}


def reply(intents):
    return json.dumps({"intents": intents, "reason": "설정 위치를 환경변수로 전환"})


class FakeProvider:
    name = "fixture"

    def __init__(self, *responses, origin=Source.REPLAY):
        self.responses = list(responses)
        self.requests: list[AIRequest] = []
        self.origin = origin

    def complete(self, request):
        self.requests.append(request)
        assert self.responses, "추가 호출 금지"
        response = self.responses.pop(0)
        if isinstance(response, DdakToolError):
            raise response
        return AIResponse(text=response, source=self.origin)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "snap"
    path.mkdir()
    (path / "config.py").write_text(ORIGINAL)
    return path


@pytest.fixture
def scans(monkeypatch):
    checked = []

    def scanner(source, patch):
        checked.append(patch)

    monkeypatch.setattr(pipeline, "strict_patch_scan", scanner)
    return checked


def context(target="cloud", enabled=True):
    return RunContext("storage-run", targets=target, toggles={"code_patch": enabled})


def propose(source, provider, ctx=None, trace=None, targets=None):
    ctx = ctx or context()
    with tool_context("patch_config", ctx.run_id):
        return generate.propose_intents(
            source,
            targets if targets is not None else scan_patch_targets(source, ()),
            ctx,
            provider=provider,
            settings=SETTINGS,
            trace=trace,
        )


def run(source, provider, *, ctx=None, review=None, facts=None, previous=None):
    ctx = ctx or context()
    session = PatchSession(ctx.run_id, source.parent, source.parent / "runs", facts, SETTINGS)
    inp = PatchConfigInput(
        run_id=ctx.run_id, source_dir=source.name, review=review, previous=previous
    )
    call = generate.call_ai
    with (
        patch_session(session),
        tool_context("patch_config", ctx.run_id),
        patch.object(generate, "call_ai", lambda **kw: call(**{**kw, "provider": provider})),
    ):
        return load_tools().get("patch_config").fn(inp, ctx)


def assert_ready(output, source, scans):
    assert output.status == "proposed" and output.passed and output.patch and output.meta
    assert output.meta.patterns == ["local_storage_dir"]
    assert output.targets == {"config.py": ["local_storage_dir"]}
    assert output.env_vars == ["IMG_DIR"]
    assert [key.name for key in output.env_keys] == ["IMG_DIR"]
    assert scans[-1] == output.patch.encode()
    assert output.patch_sha256 == digest_bytes(output.patch.encode())
    prepared = PatchPreparation.from_output(output)
    encoded = json.loads(encode_meta(prepared.meta))
    assert encoded["reason"] == output.meta.reason
    assert encoded["source"] == output.meta.source.value
    assert encoded["passed"] and encoded["patch_sha256"] == output.patch_sha256
    assert prepared.patch == output.patch.encode()
    assert PatchConfigOutput.model_validate_json(output.model_dump_json()) == output
    assert (source / "config.py").read_text() == ORIGINAL


@pytest.mark.parametrize("target", [None, "both", "cloud"])
def test_missing_storage_retries_then_rules_are_byte_deterministic(source, scans, target):
    provider = FakeProvider(reply([]), reply([]), reply([]), reply([]))
    outputs = [run(source, provider, ctx=context(target)) for _ in range(2)]
    for output in outputs:
        assert_ready(output, source, scans)
        assert output.attempts == 2 and output.source is Source.REPLAY
        assert output.meta.source is Source.REPLAY and "규칙으로 보완" in output.meta.reason
    assert outputs[0].patch.encode() == outputs[1].patch.encode()
    assert len(provider.requests) == 4
    feedback = provider.requests[1].user.split("필수 저장 위치 누락: ")[1].splitlines()[0]
    assert json.loads(feedback) == [
        {"file": "config.py", "line": 1, "pattern_id": "local_storage_dir"}
    ]
    assert all(MARKER not in r.user + r.system for r in provider.requests)
    assert "IMG_DIR" not in feedback
    targets = scan_patch_targets(source, ())
    expected, _ = render_intents(source, targets, [EditIntent(**STORAGE_INTENT)])
    assert outputs[0].patch.encode() == expected


@pytest.mark.parametrize("origin", [Source.LIVE, Source.FIXTURE, Source.REPLAY, Source.CACHE])
def test_second_response_completes_storage_without_fallback(source, scans, origin):
    provider = FakeProvider(reply([]), reply([STORAGE_INTENT]), origin=origin)
    output = run(source, provider)
    assert_ready(output, source, scans)
    assert output.attempts == 2 and output.source is output.meta.source is origin
    assert "규칙으로 보완" not in output.meta.reason
    assert len(provider.requests) == 2


def test_storage_present_in_first_reply_needs_no_retry(source, scans):
    provider = FakeProvider(reply([STORAGE_INTENT]))
    output = run(source, provider)
    assert_ready(output, source, scans)
    assert output.attempts == 1 and "규칙으로 보완" not in output.meta.reason


def test_other_valid_intents_are_preserved_when_only_storage_is_missing(source, scans):
    (source / "secret.py").write_text("SECRET_KEY = 'dev'\n")
    provider = FakeProvider(reply([SECRET_INTENT]), reply([SECRET_INTENT]))
    output = run(source, provider)
    assert output.passed and output.patch and output.meta
    assert output.env_vars == ["IMG_DIR", "SECRET_KEY"]
    assert output.meta.source is Source.REPLAY and "규칙으로 보완" in output.meta.reason
    assert output.changed_files == ["config.py", "secret.py"]
    assert len(provider.requests) == 2 and scans == [output.patch.encode()]


def test_second_complete_reply_does_not_use_first_completion_candidate(source, scans):
    (source / "secret.py").write_text("SECRET_KEY = 'dev'\n")
    provider = FakeProvider(reply([SECRET_INTENT]), reply([SECRET_INTENT, STORAGE_INTENT]))
    output = run(source, provider)
    assert output.passed and output.meta and "규칙으로 보완" not in output.meta.reason
    assert output.attempts == 2 and len(scans) == 1


@pytest.mark.parametrize("text", ["not json", reply([{**STORAGE_INTENT, "key": "OTHER"}])])
def test_invalid_responses_only_fallback_to_the_known_storage_location(source, scans, text):
    output = run(source, FakeProvider(text, text))
    assert_ready(output, source, scans)
    assert output.attempts == 2 and "규칙으로 보완" in output.meta.reason


def test_provider_error_completes_storage_without_extra_calls(source, scans):
    provider = FakeProvider(DdakToolError(ErrorCode.AI_UNAVAILABLE, "fixture unavailable"))
    output = run(source, provider)
    assert_ready(output, source, scans)
    assert output.attempts == 1 and output.source is None
    assert output.meta.source is Source.CACHE and "규칙으로 보완" in output.meta.reason
    assert len(provider.requests) == 1


def test_error_after_empty_reply_preserves_actual_response_source(source, scans):
    provider = FakeProvider(
        reply([]),
        DdakToolError(ErrorCode.AI_UNAVAILABLE, "fixture unavailable"),
        origin=Source.FIXTURE,
    )
    output = run(source, provider)
    assert_ready(output, source, scans)
    assert output.attempts == 2 and output.source is output.meta.source is Source.FIXTURE


def test_onprem_empty_reply_remains_empty_without_retry(source, scans):
    provider = FakeProvider(reply([]))
    output = run(source, provider, ctx=context("onprem"))
    assert output.status == "no_targets" and output.patch is None and output.meta is None
    assert output.attempts == 1 and len(provider.requests) == 1 and scans == []


@pytest.mark.parametrize("version", [1, 2])
def test_no_storage_evidence_never_adds_img_dir(source, scans, version):
    (source / "config.py").write_text(f"VERSION = {version}\nSECRET_KEY = 'dev'\n")
    assert scan_storage({"config.py": (source / "config.py").read_text()}) == []
    provider = FakeProvider(reply([]))
    output = run(source, provider)
    assert output.status == "no_targets" and output.attempts == 1
    assert output.patch is None and output.env_vars == [] and scans == []


@pytest.mark.parametrize("text", ["VERSION = 1\n", "VERSION = 2\n"])
def test_no_targets_does_not_call_provider(source, scans, text):
    (source / "config.py").write_text(text)
    provider = FakeProvider()
    output = run(source, provider)
    assert output.status == "no_targets" and output.patch is None
    assert output.attempts == 0 and provider.requests == [] and scans == []


def test_storage_already_in_environment_is_not_rule_patched(source, scans):
    (source / "config.py").write_text("import os\nIMG_DIR = os.environ['IMG_DIR']\n")
    output = run(source, FakeProvider())
    assert output.status == "no_targets" and output.attempts == 0 and scans == []


@pytest.mark.parametrize("target", [None, "both", "cloud"])
def test_toggle_off_blocks_unpatched_storage_without_new_proposal(source, scans, target):
    provider = FakeProvider()
    with pytest.raises(DdakToolError) as blocked:
        run(source, provider, ctx=context(target, enabled=False))
    assert blocked.value.code is ErrorCode.PRECONDITION_FAILED
    assert blocked.value.message == "클라우드 IMG_DIR 패치 필요, 코드수정 켜기"
    assert provider.requests == [] and scans == []
    with pytest.raises(DdakToolError) as error:
        propose(source, provider, ctx=context(enabled=False))
    assert error.value.code is ErrorCode.TOGGLE_OFF


@pytest.mark.parametrize("storage", [False, True])
def test_other_patterns_and_onprem_keep_toggle_off_behavior(source, scans, storage):
    (source / "secret.py").write_text("SECRET_KEY = 'dev'\n")
    if not storage:
        (source / "config.py").write_text("VERSION = 1\n")
    provider = FakeProvider()
    output = run(source, provider, ctx=context("onprem" if storage else "cloud", enabled=False))
    assert output.status == "no_targets" and output.patch is None
    assert output.attempts == 0 and provider.requests == [] and scans == []


def test_toggle_off_reuses_existing_storage_patch_without_provider(source, scans):
    first = run(source, FakeProvider(reply([STORAGE_INTENT])))
    approved = ApprovedPatch(patch=first.patch, reason="승인된 저장 경로", source=Source.REPLAY)
    provider = FakeProvider()
    output = run(source, provider, ctx=context(enabled=False), previous=approved)
    assert output.status == "reused" and output.passed and output.patch == first.patch
    assert output.attempts == 0 and provider.requests == []


def test_toggle_off_blocks_storage_when_approved_patch_cannot_be_reused(source, scans):
    first = run(source, FakeProvider(reply([STORAGE_INTENT])))
    approved = ApprovedPatch(patch=first.patch, reason="승인된 저장 경로", source=Source.REPLAY)
    (source / "config.py").write_text(ORIGINAL + "VERSION = 2\n")
    provider = FakeProvider()
    with pytest.raises(DdakToolError) as blocked:
        run(source, provider, ctx=context(enabled=False), previous=approved)
    assert blocked.value.code is ErrorCode.PRECONDITION_FAILED
    assert blocked.value.message == "클라우드 IMG_DIR 패치 필요, 코드수정 켜기"
    assert provider.requests == [] and len(scans) == 1


def test_toggle_off_allows_storage_already_reading_environment(source, scans):
    (source / "config.py").write_text("import os\nIMG_DIR = os.environ['IMG_DIR']\n")
    provider = FakeProvider()
    output = run(source, provider, ctx=context(enabled=False))
    assert output.status == "no_targets" and output.attempts == 0
    assert output.patch is None and provider.requests == [] and scans == []


@pytest.mark.parametrize("failure", ["empty", "error", "missing"])
def test_pipeline_cannot_continue_without_required_storage_patch(source, failure):
    calls = []

    def proposer(*args):
        calls.append(args)
        if failure == "error":
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "fixture unavailable")
        return b"", (), Source.REPLAY.value

    with pytest.raises(DdakToolError, match="IMG_DIR"):
        prepare_patch(
            source,
            None,
            context(),
            previous={},
            runs_root=source.parent / "runs",
            proposer=None if failure == "missing" else proposer,
            scanner=lambda *_: None,
        )
    assert len(calls) == (0 if failure == "missing" else 1)


@pytest.mark.parametrize("check_failure", [False, True])
def test_existing_checker_and_secret_scanner_fail_closed(source, monkeypatch, check_failure):
    called = []

    def scanner(*args):
        called.append(args)
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "fixture scan rejected")

    if check_failure:
        from ddak.plan.patch.check import PatchCheck

        monkeypatch.setattr(
            pipeline, "check_patch", lambda *_: PatchCheck(passed=False, patch_sha256=None)
        )
    monkeypatch.setattr(pipeline, "strict_patch_scan", scanner)
    with pytest.raises(DdakToolError, match="IMG_DIR"):
        run(source, FakeProvider(reply([]), reply([])))
    assert len(called) == (0 if check_failure else 1)


def test_analysis_omission_is_reconciled_from_storage_evidence(source, scans):
    ctx = context()
    facts = Facts(
        project="demo",
        mode=ctx.mode,
        target="cloud",
        tiers=("was",),
        changed={"cloud": {"was": True}},
        code_patch=True,
        patch_targets=(),
        source_snapshot_hash=digest_bytes(b"source"),
        facts_hash=digest_bytes(b"facts"),
    )
    output = run(source, FakeProvider(reply([]), reply([])), facts=facts)
    assert_ready(output, source, scans)
    assert output.attempts == 2


def test_unsupported_storage_sites_do_not_relax_renderer(source):
    (source / "other.py").write_text(ORIGINAL)
    with pytest.raises(DdakToolError, match="규칙 보완"):
        propose(source, FakeProvider(reply([]), reply([])))


def test_storage_review_keeps_rule_reason_and_required_flag(source, scans):
    provider = FakeProvider(reply([]), reply([]))
    output = run(source, provider, review=PatchReviewRequest(action="propose"))
    assert_ready(output, source, scans)
    assert len(output.proposals) == 1 and output.proposals[0].required
    assert "규칙으로 보완" in output.proposals[0].reason
    composed = run(
        source,
        provider,
        review=PatchReviewRequest(
            action="compose", proposals=output.proposals, selected=[output.proposals[0].id]
        ),
    )
    assert_ready(composed, source, scans)
    assert composed.patch == output.patch and composed.attempts == 0
    assert composed.source is None and composed.meta.source is Source.CACHE
    assert "규칙으로 보완" in composed.meta.reason and len(provider.requests) == 2


@pytest.mark.parametrize("tamper", ["omit", "unmark", "remove"])
def test_storage_review_cannot_drop_required_patch(source, scans, tamper):
    provider = FakeProvider(reply([STORAGE_INTENT]))
    first = run(source, provider, review=PatchReviewRequest(action="propose"))
    proposals = first.proposals
    selected = [proposals[0].id]
    if tamper == "omit":
        selected = []
    elif tamper == "unmark":
        proposals = [proposals[0].model_copy(update={"required": False})]
    else:
        proposals, selected = [], []
    with pytest.raises(DdakToolError):
        run(
            source,
            provider,
            review=PatchReviewRequest(action="compose", proposals=proposals, selected=selected),
        )
    assert len(provider.requests) == 1


def test_rule_patch_applies_only_to_expected_literal(source, scans, tmp_path):
    provider = FakeProvider(reply([]), reply([]))
    output = run(source, provider)
    built = tmp_path / "candidate"
    copy_source(source, built)
    apply_diff(built, output.patch.encode())
    assert (built / "config.py").read_text() == "import os\nIMG_DIR = os.environ['IMG_DIR']\n"
    assert all(
        e.kind != "hardcoded_dir"
        for e in scan_storage({"config.py": (built / "config.py").read_text()})
    )
