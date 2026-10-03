"""source=fixture: 새 생성기는 위치만 전송하고 결정적 렌더러를 사용한다."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import ddak.plan.patch as public
from ddak.core.ai.gateway import DATA_CLOSE, DATA_OPEN
from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.config import LLMBackend, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan_facts import PatchTarget
from ddak.core.patch_patterns import scan_patch_targets
from ddak.core.runtime import current_run_id, current_tool, tool_context
from ddak.core.snapshots import apply_diff, copy_source, file_manifest
from ddak.plan.patch.check import check_patch
from ddak.plan.patch.generate import (
    PatchDraft,
    PreviousPatch,
    find_targets,
    propose_config_patch,
)
from ddak.plan.patch.generate import (
    propose_intents as _propose_intents,
)
from ddak.plan.patch.intents import EditIntent, render_intents

CFG = Settings(ai_retries=0, llm_backend=LLMBackend.API, llm_model="fixture-model")
ON = RunContext("run-intents", toggles={"code_patch": True})
MARKER = "fixture-" + "private-value-" + "must-not-leave"
REASON = "배포 설정을 필수 환경변수로 전환한다"
DEFAULT_KEYS = frozenset(
    {
        "SECRET_KEY",
        "APP_BASE_URL",
        "DATABASE_URL",
        "SESSION_COOKIE_SECURE",
        "PROXY_FIX_X_FOR",
        "PROXY_FIX_X_PROTO",
    }
)


def propose_intents(source, allowed, ctx, **kwargs):
    """앱이 소유할 호출자 wrapper. 생성기 자체는 이 문맥을 만들지 않는다."""
    with tool_context("patch_config", ctx.run_id):
        return _propose_intents(source, allowed, ctx, **kwargs)


class FakeProvider:
    name = "fixture"

    def __init__(self, *replies: str | DdakToolError, origin: Source = Source.FIXTURE) -> None:
        self.replies = list(replies)
        self.origin = origin
        self.seen: list[AIRequest] = []
        self.contexts: list[tuple[str | None, str | None]] = []

    def complete(self, request: AIRequest) -> AIResponse:
        self.seen.append(request)
        self.contexts.append((current_tool.get(), current_run_id.get()))
        assert self.replies, "허용된 횟수를 넘어 provider를 호출했다"
        response = self.replies.pop(0)
        if isinstance(response, DdakToolError):
            raise response
        return AIResponse(text=response, source=self.origin)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    text = (
        f'"""원본 문서 {MARKER}"""\n'
        f"SECRET_KEY = '{MARKER}'\n"
        "SESSION_COOKIE_SECURE = False\n"
        f"APP_BASE_URL = 'http://localhost:5000/{MARKER}'\n"
    )
    (root / "app.py").write_bytes(text.encode())
    return root


def targets(source: Path) -> tuple[PatchTarget, ...]:
    return scan_patch_targets(source, ["app.py"])


def draft(allowed: tuple[PatchTarget, ...]) -> dict:
    return {
        "intents": [
            {
                "file": t.file,
                "line": t.line,
                "pattern_id": t.pattern_id,
                "key": t.key or "APP_BASE_URL",
            }
            for t in allowed
            if t.severity == "patch"
        ],
        "reason": REASON,
    }


def reply(allowed: tuple[PatchTarget, ...]) -> str:
    return json.dumps(draft(allowed), ensure_ascii=False)


def payload(request: AIRequest) -> dict:
    return json.loads(request.user.split(DATA_OPEN + "\n", 1)[1].split("\n" + DATA_CLOSE, 1)[0])


def test_new_api_sends_only_positions_and_uses_real_renderer(source: Path, tmp_path: Path) -> None:
    allowed = targets(source)
    before = file_manifest(source)
    provider = FakeProvider(reply(allowed))
    patch, keys, origin = propose_intents(source, allowed, ON, settings=CFG, provider=provider)
    assert len(provider.seen) == 1
    assert origin == "fixture"
    expected = render_intents(
        source, allowed, [EditIntent.model_validate(i) for i in draft(allowed)["intents"]]
    )
    assert (patch, keys) == expected
    assert provider.contexts == [("patch_config", ON.run_id)]
    request = provider.seen[0]
    assert request.purpose == "patch_config"
    assert request.prompt_version == "patch_config-intents-v3"
    assert request.model == CFG.llm_model
    assert set(request.json_schema["properties"]) == {"intents", "reason"}
    assert request.json_schema["additionalProperties"] is False
    assert payload(request) == {
        "allowed_keys": sorted(DEFAULT_KEYS),
        "targets": [t.model_dump(include={"file", "line", "pattern_id", "key"}) for t in allowed],
    }
    assert MARKER not in request.user
    assert "localhost" not in json.dumps(payload(request))
    assert str(source) not in request.user
    assert "[REDACTED]" not in request.user  # 원문을 가려 보내는 경로도 쓰지 않는다.
    checked = check_patch(source, patch)
    assert checked.passed, checked.violations
    built = tmp_path / "built"
    copy_source(source, built)
    apply_diff(built, patch)
    text = (built / "app.py").read_text()
    assert "SECRET_KEY = os.environ['SECRET_KEY']" in text
    assert "SESSION_COOKIE_SECURE = (os.environ['SESSION_COOKIE_SECURE'].lower() == 'true')" in text
    assert "APP_BASE_URL = os.environ['APP_BASE_URL']" in text
    assert file_manifest(source) == before


def test_caller_context_is_restored(source: Path) -> None:
    provider = FakeProvider(reply(targets(source)))
    with tool_context("health_check", "outer-run"):
        propose_intents(source, targets(source), ON, settings=CFG, provider=provider)
        assert current_tool.get() == "health_check"
        assert current_run_id.get() == "outer-run"
    assert provider.contexts == [("patch_config", ON.run_id)]
    assert current_tool.get() is None
    assert current_run_id.get() is None


def test_run_context_is_not_serialized(source: Path) -> None:
    ctx = RunContext(
        "run-context-" + MARKER,
        toggles={"code_patch": True},
        platform={"value": MARKER},
        cloud_domain=MARKER,
        project_settings={"value": MARKER},
    )
    provider = FakeProvider(reply(targets(source)))
    propose_intents(source, targets(source), ctx, settings=CFG, provider=provider)
    assert MARKER not in provider.seen[0].user
    assert provider.contexts == [("patch_config", ctx.run_id)]


def test_new_path_never_uses_legacy_source_prompt_helpers(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("기존 원문/코드 편집 생성기 경로가 호출됐다")

    for name in ("_read", "_numbered", "find_targets", "apply_edits", "build_patch"):
        monkeypatch.setattr("ddak.plan.patch.generate." + name, forbidden)
    allowed = targets(source)
    patch, _, _ = propose_intents(
        source, allowed, ON, settings=CFG, provider=FakeProvider(reply(allowed))
    )
    assert patch


def test_toggle_off_never_calls_ai(source: Path) -> None:
    provider = FakeProvider()
    with pytest.raises(DdakToolError) as caught:
        propose_intents(source, targets(source), RunContext("off"), settings=CFG, provider=provider)
    assert caught.value.code is ErrorCode.TOGGLE_OFF
    assert provider.seen == []


def test_no_patch_targets_is_noop_without_reading_source(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    warning = PatchTarget(
        file="app.py",
        line=1,
        pattern_id="local_address",
        key="APP_BASE_URL",
        is_new=True,
        severity="warning",
    )
    provider = FakeProvider()
    assert propose_intents(missing, (), ON, settings=CFG, provider=provider) == (b"", (), "rule")
    assert propose_intents(missing, (warning,), ON, settings=CFG, provider=provider) == (
        b"",
        (),
        "rule",
    )
    assert provider.seen == []


def test_warnings_are_not_sent_or_edited(source: Path) -> None:
    private = source / "private.py"
    private.write_bytes(b"APP_BASE_URL = '10.0.0.1'\n")
    allowed = scan_patch_targets(source, ["app.py", "private.py"])
    provider = FakeProvider(reply(allowed))
    patch, _, _ = propose_intents(source, allowed, ON, settings=CFG, provider=provider)
    assert all(t["file"] == "app.py" for t in payload(provider.seen[0])["targets"])
    assert b"private.py" not in patch
    assert private.read_bytes() == b"APP_BASE_URL = '10.0.0.1'\n"


@pytest.mark.parametrize(
    "invalid",
    [
        "json",
        "code",
        "value",
        "lines",
        "line_type",
        "extra",
        "reason_type",
        "reason_missing",
        "reason_long",
        "reason_empty",
        "duplicate",
        "missing",
        "unknown_file",
        "unknown_line",
        "unknown_pattern",
        "wrong_key",
    ],
)
def test_invalid_output_retries_once_without_echoing_source(source: Path, invalid: str) -> None:
    allowed = targets(source)
    bad = draft(allowed)
    item = bad["intents"][0]
    if invalid == "json":
        bad_reply = "{" + MARKER
    else:
        if invalid in {"code", "value", "lines"}:
            item[invalid] = MARKER
        elif invalid == "line_type":
            item["line"] = str(item["line"])
        elif invalid == "extra":
            bad["patch"] = MARKER
        elif invalid == "reason_type":
            bad["reason"] = 123
        elif invalid == "reason_missing":
            del bad["reason"]
        elif invalid == "reason_long":
            bad["reason"] = "가" * 201
        elif invalid == "reason_empty":
            bad["reason"] = ""
        elif invalid == "duplicate":
            bad["intents"].append(dict(item))
        elif invalid == "missing":
            bad["intents"].pop()
        elif invalid == "unknown_file":
            item["file"] = "other.py"
        elif invalid == "unknown_line":
            item["line"] = 5000
        elif invalid == "unknown_pattern":
            item["pattern_id"] = "new_arbitrary_pattern"
        elif invalid == "wrong_key":
            item["key"] = "OTHER_KEY"
        bad_reply = json.dumps(bad)
    provider = FakeProvider(bad_reply, reply(allowed))
    patch, _, _ = propose_intents(source, allowed, ON, settings=CFG, provider=provider)
    assert patch and len(provider.seen) == 2
    assert payload(provider.seen[0]) == payload(provider.seen[1])
    assert "AI_OUTPUT_INVALID" in provider.seen[1].user
    assert all(MARKER not in req.user and bad_reply not in req.user for req in provider.seen)


def test_second_rejection_is_typed_error_with_no_private_details(source: Path) -> None:
    provider = FakeProvider("{" + MARKER, "{" + MARKER, reply(targets(source)))
    with tool_context("outer-tool", "outer-run"):
        with pytest.raises(DdakToolError) as caught:
            propose_intents(source, targets(source), ON, settings=CFG, provider=provider)
        assert current_tool.get() == "outer-tool"
        assert current_run_id.get() == "outer-run"
    assert caught.value.code is ErrorCode.AI_OUTPUT_INVALID
    assert MARKER not in str(caught.value)
    assert len(provider.seen) == 2 and len(provider.replies) == 1


@pytest.mark.parametrize("origin", [Source.LIVE, Source.REPLAY, Source.FIXTURE, Source.CACHE])
def test_origin_is_preserved(source: Path, origin: Source) -> None:
    provider = FakeProvider(reply(targets(source)), origin=origin)
    _, _, label = propose_intents(source, targets(source), ON, settings=CFG, provider=provider)
    assert label == origin.value


def test_provider_unavailable_is_not_retried_as_invalid_output(source: Path) -> None:
    provider = FakeProvider(DdakToolError(ErrorCode.AI_UNAVAILABLE, "fixture unavailable"))
    with pytest.raises(DdakToolError) as caught:
        propose_intents(source, targets(source), ON, settings=CFG, provider=provider)
    assert caught.value.code is ErrorCode.AI_UNAVAILABLE
    assert len(provider.seen) == 1


def test_existing_envkey_theft_is_invalid_output(source: Path) -> None:
    (source / "existing.py").write_bytes(b"import os\nvalue = os.environ['APP_BASE_URL']\n")
    allowed = targets(source)
    provider = FakeProvider(reply(allowed), reply(allowed))
    with pytest.raises(DdakToolError) as caught:
        propose_intents(source, allowed, ON, settings=CFG, provider=provider)
    assert caught.value.code is ErrorCode.AI_OUTPUT_INVALID
    assert len(provider.seen) == 2
    assert all(MARKER not in request.user for request in provider.seen)


def test_invalid_target_contract_fails_before_ai(source: Path) -> None:
    provider = FakeProvider()
    with pytest.raises(DdakToolError) as caught:
        propose_intents(source, ({"file": "app.py"},), ON, settings=CFG, provider=provider)
    assert caught.value.code is ErrorCode.PRECONDITION_FAILED
    assert provider.seen == []


@pytest.mark.parametrize("field", ["file", "pattern_id", "key"])
def test_malformed_target_metadata_is_never_sent(source: Path, field: str) -> None:
    item = targets(source)[0].model_dump()
    item[field] = MARKER
    bad = PatchTarget.model_validate(item)
    provider = FakeProvider()
    with pytest.raises(DdakToolError) as caught:
        propose_intents(source, (bad,), ON, settings=CFG, provider=provider)
    assert caught.value.code is ErrorCode.PRECONDITION_FAILED
    assert MARKER not in str(caught.value)
    assert provider.seen == []


def test_unknown_target_key_is_chosen_without_source_input(source: Path) -> None:
    (source / "app.py").write_bytes(b"connect('localhost')\n")
    allowed = targets(source)
    assert len(allowed) == 1 and allowed[0].key is None
    provider = FakeProvider(reply(allowed))
    patch, keys, _ = propose_intents(source, allowed, ON, settings=CFG, provider=provider)
    assert payload(provider.seen[0])["targets"][0]["key"] is None
    assert [key.name for key in keys] == ["APP_BASE_URL"]
    checked = check_patch(source, patch)
    assert checked.passed, checked.violations


def test_source_read_failure_is_not_ai_retry(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allowed = targets(source)

    def unavailable(*args):
        raise PermissionError(MARKER)

    monkeypatch.setattr("ddak.plan.patch.generate.render_intents", unavailable)
    provider = FakeProvider(reply(allowed))
    with pytest.raises(DdakToolError) as caught:
        propose_intents(source, allowed, ON, settings=CFG, provider=provider)
    assert caught.value.code is ErrorCode.PRECONDITION_FAILED
    assert MARKER not in str(caught.value)
    assert len(provider.seen) == 1


def test_old_and_new_public_exports_are_preserved_once() -> None:
    expected = {
        "PatchDraft",
        "PatchEdit",
        "PatchProposal",
        "PreviousPatch",
        "find_targets",
        "propose_config_patch",
        "patch_config",
        "patch_db_access",
        "patch_storage",
        "EditIntent",
        "render_intents",
        "propose_intents",
        "PatchPreparation",
        "PatchSession",
        "patch_session",
        "prepare_patch",
    }
    assert set(public.__all__) == expected
    assert len(public.__all__) == len(expected)
    assert all(hasattr(public, name) for name in expected)
    assert public.propose_intents is _propose_intents
    tree = ast.parse(Path(public.__file__).read_text())
    exports = [
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets)
    ]
    assert len(exports) == 1


@pytest.mark.parametrize("tool", [None, "health_check", "analyze_project"])
def test_generator_cannot_create_or_replace_caller_tool_context(
    source: Path, tool: str | None
) -> None:
    provider = FakeProvider(reply(targets(source)))
    with tool_context(tool, "caller-run"):
        with pytest.raises(DdakToolError) as caught:
            _propose_intents(source, targets(source), ON, settings=CFG, provider=provider)
        assert current_tool.get() == tool
        assert current_run_id.get() == "caller-run"
    assert caught.value.code is ErrorCode.AI_NOT_ALLOWED
    assert provider.seen == []


def test_default_key_allowlist_rejects_arbitrary_keys_then_retries(source: Path) -> None:
    (source / "app.py").write_bytes(b"connect('localhost')\n")
    allowed = targets(source)
    bad = draft(allowed)
    bad["intents"][0]["key"] = "UNAUTHORIZED_KEY"
    provider = FakeProvider(json.dumps(bad), reply(allowed))
    patch, keys, _ = propose_intents(source, allowed, ON, settings=CFG, provider=provider)
    assert patch and [k.name for k in keys] == ["APP_BASE_URL"]
    assert len(provider.seen) == 2
    assert payload(provider.seen[0])["allowed_keys"] == sorted(DEFAULT_KEYS)


def test_env_example_contributes_only_names_without_values(source: Path) -> None:
    (source / "app.py").write_bytes(b"HOST = 'localhost'\n")
    example = source / ".env.example"
    example.write_text(
        f"HOST={MARKER}\nexport EXTRA_URL='{MARKER}'\nDATABASE_PASSWORD_MIGRATOR={MARKER}\n"
        f"# COMMENTED_KEY={MARKER}\nlowercase={MARKER}\n",
        encoding="utf-8",
    )
    ctx = RunContext(
        "template", toggles={"code_patch": True}, deploy_config={"env_example": ".env.example"}
    )
    allowed = targets(source)
    provider = FakeProvider(reply(allowed))
    patch, keys, _ = propose_intents(source, allowed, ctx, settings=CFG, provider=provider)
    sent = payload(provider.seen[0])
    assert sent["allowed_keys"] == sorted(DEFAULT_KEYS | {"HOST", "EXTRA_URL"})
    assert MARKER not in provider.seen[0].user
    assert "DATABASE_PASSWORD_MIGRATOR" not in sent["allowed_keys"]
    assert [k.name for k in keys] == ["HOST"]
    assert b"os.environ['HOST']" in patch
    assert b".get(" not in patch and b"getenv(" not in patch
    assert MARKER not in patch.decode()


def test_migration_key_is_invalid_even_if_listed_in_env_example(source: Path) -> None:
    (source / "app.py").write_bytes(b"connect('localhost')\n")
    (source / ".env.example").write_text("DB_PASSWORD_MIGRATOR=\n", encoding="utf-8")
    ctx = RunContext(
        "migrate-key", toggles={"code_patch": True}, deploy_config={"env_example": ".env.example"}
    )
    allowed = targets(source)
    bad = draft(allowed)
    bad["intents"][0]["key"] = "DB_PASSWORD_MIGRATOR"
    provider = FakeProvider(json.dumps(bad), json.dumps(bad))
    with pytest.raises(DdakToolError) as caught:
        propose_intents(source, allowed, ctx, settings=CFG, provider=provider)
    assert caught.value.code is ErrorCode.AI_OUTPUT_INVALID
    assert len(provider.seen) == 2
    assert all("DB_PASSWORD_MIGRATOR" not in payload(r)["allowed_keys"] for r in provider.seen)


def test_missing_env_example_uses_five_default_keys(source: Path) -> None:
    ctx = RunContext(
        "missing-example",
        toggles={"code_patch": True},
        deploy_config={"env_example": "missing.example"},
    )
    provider = FakeProvider(reply(targets(source)))
    propose_intents(source, targets(source), ctx, settings=CFG, provider=provider)
    assert payload(provider.seen[0])["allowed_keys"] == sorted(DEFAULT_KEYS)


@pytest.mark.parametrize(
    "example", [".env", ".env.production", "../outside", "/absolute", "key.pem"]
)
def test_unsafe_env_example_is_rejected_before_ai(source: Path, example: str) -> None:
    ctx = RunContext(
        "unsafe-example", toggles={"code_patch": True}, deploy_config={"env_example": example}
    )
    provider = FakeProvider()
    with pytest.raises(DdakToolError) as caught:
        propose_intents(source, targets(source), ctx, settings=CFG, provider=provider)
    assert caught.value.code is ErrorCode.PRECONDITION_FAILED
    assert provider.seen == []


def test_symlink_env_example_is_not_read(source: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.example"
    outside.write_text(f"UNAUTHORIZED_KEY={MARKER}\n", encoding="utf-8")
    (source / ".env.example").symlink_to(outside)
    ctx = RunContext(
        "linked-example",
        toggles={"code_patch": True},
        deploy_config={"env_example": ".env.example"},
    )
    provider = FakeProvider()
    with pytest.raises(DdakToolError) as caught:
        propose_intents(source, targets(source), ctx, settings=CFG, provider=provider)
    assert caught.value.code is ErrorCode.PRECONDITION_FAILED
    assert provider.seen == []


def legacy_reply(*, bad: bool = False, empty: bool = False) -> str:
    expression = "os.environ.get('SECRET_KEY', 'dev')" if bad else "os.environ['SECRET_KEY']"
    edits = (
        []
        if empty
        else [
            {"path": "app.py", "start": 1, "end": 0, "lines": ["import os"]},
            {"path": "app.py", "start": 2, "end": 2, "lines": [f"SECRET_KEY = {expression}"]},
        ]
    )
    return json.dumps({"edits": edits, "reason": MARKER, "env_vars": ["SECRET_KEY"]})


def legacy(source: Path, provider: FakeProvider, **kwargs):
    with tool_context("patch_config", ON.run_id):
        return propose_config_patch(source, ON, settings=CFG, provider=provider, **kwargs)


def test_legacy_rejected_candidate_never_returns_patch(source: Path) -> None:
    result = legacy(source, FakeProvider(legacy_reply(bad=True), legacy_reply(bad=True)))
    assert result.status == "rejected" and result.patch is None and result.meta is None
    assert result.check is not None and not result.check.passed
    assert result.attempts == 2


def test_legacy_invalid_schema_retries_once_and_returns_checked_patch(source: Path) -> None:
    provider = FakeProvider("invalid-json", legacy_reply())
    result = legacy(source, provider)
    assert result.status == "proposed" and result.patch
    assert result.check is not None and result.check.passed
    assert result.attempts == 2 and len(provider.seen) == 2
    assert "AI_OUTPUT_INVALID" in provider.seen[1].user


def test_legacy_invalid_schema_exhaustion_returns_rejected(source: Path) -> None:
    provider = FakeProvider("invalid-json", "invalid-json", legacy_reply())
    result = legacy(source, provider)
    assert result.status == "rejected" and result.patch is None and result.meta is None
    assert result.attempts == 2 and len(provider.seen) == 2


def test_legacy_unavailable_returns_rejected(source: Path) -> None:
    result = legacy(
        source, FakeProvider(DdakToolError(ErrorCode.AI_UNAVAILABLE, "fixture unavailable"))
    )
    assert result.status == "rejected" and result.patch is None and result.attempts == 1


def test_legacy_numbered_precondition_returns_rejected_without_ai(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ddak.plan.patch.generate.redact", lambda text: "[REDACTED]")
    provider = FakeProvider()
    result = legacy(source, provider)
    assert result.status == "rejected" and result.patch is None and result.attempts == 0
    assert provider.seen == []


def test_legacy_empty_edits_are_no_targets(source: Path) -> None:
    assert PatchDraft.model_validate_json(legacy_reply(empty=True)).edits == []
    provider = FakeProvider(legacy_reply(empty=True))
    result = legacy(source, provider)
    assert result.status == "no_targets" and result.patch is None and result.check is None
    assert result.attempts == 1


def test_legacy_meta_reason_is_code_owned_for_new_and_reused_patch(source: Path) -> None:
    first = legacy(source, FakeProvider(legacy_reply()))
    assert first.check is not None and first.check.patterns == ["secret_key"]
    assert first.meta["reason"] == "환경변수 전환: 서명 키"
    assert MARKER not in str(first.meta)
    reused = legacy(source, FakeProvider(), previous=PreviousPatch(first.patch, MARKER))
    assert reused.status == "reused" and reused.meta["reason"] == first.meta["reason"]
    assert MARKER not in str(reused.meta)


@pytest.mark.parametrize("tool", [None, "health_check", "analyze_project"])
def test_legacy_ai_guard_is_not_absorbed(source: Path, tool: str | None) -> None:
    provider = FakeProvider(legacy_reply())
    with tool_context(tool, ON.run_id), pytest.raises(DdakToolError) as caught:
        propose_config_patch(source, ON, settings=CFG, provider=provider)
    assert caught.value.code is ErrorCode.AI_NOT_ALLOWED
    assert provider.seen == []


def test_legacy_find_targets_ports_815ea60_env_read_exclusions(source: Path) -> None:
    (source / "app.py").write_text(
        'X = {"PROXY_FIX_X_FOR": env_int("PROXY_FIX_X_FOR", 0)}\n'
        'Y = os.environ.get("SECRET_KEY")\n'
        'x_for, x_proto = app.config["PROXY_FIX_X_FOR"], app.config["PROXY_FIX_X_PROTO"]\n'
        "app.wsgi_app = ProxyFix(app.wsgi_app, x_for=x_for, x_proto=x_proto)\n",
        encoding="utf-8",
    )
    assert find_targets(source) == {}
    result = legacy(source, FakeProvider())
    assert result.status == "no_targets" and result.attempts == 0


def test_legacy_find_targets_ports_815ea60_hardcoded_literals(source: Path) -> None:
    (source / "app.py").write_text(
        'app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)\nURL = f"http://localhost:{PORT}"\n',
        encoding="utf-8",
    )
    assert find_targets(source) == {"app.py": ["local_address", "proxy_fix"]}
