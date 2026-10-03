"""patch_config 툴 입출력: 원본 경로, 상태별 출력 칸, 실행기 prepare 메타 모양, 레지스트리 등록."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ddak.app import load_tools
from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.config import LLMBackend, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.patch_config import (
    ApprovedPatch,
    PatchConfigInput,
    PatchConfigOutput,
)
from ddak.core.runtime import tool_context
from ddak.core.snapshots import digest_bytes
from ddak.executor.approval_meta import encode_meta
from ddak.plan.patch import patch_config

CONFIG = "flaskr/config.py"
# 샘플 앱 config.load() 모양(10/3 실제 Claude 시험의 후보 앱과 같다)
ORIGINAL = """import os


def load():
    return {
        "APP_BASE_URL": "http://localhost:5000",
        "SECRET_KEY": "dev",
    }
"""
INTENTS = [
    {"file": CONFIG, "line": 6, "pattern_id": "local_address", "key": "APP_BASE_URL"},
    {"file": CONFIG, "line": 7, "pattern_id": "secret_key", "key": "SECRET_KEY"},
]

CFG = Settings(ai_retries=0, llm_backend=LLMBackend.API, llm_model="m-claude")
ON = RunContext("run-1", toggles={"code_patch": True})


def reply(intents: list[dict[str, object]]) -> str:
    return json.dumps(
        {
            "intents": intents,
            "reason": "주소와 서명 키를 환경변수에서 읽게 바꿨다",
        }
    )


class FakeProvider:
    name = "fake"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)

    def complete(self, req: AIRequest) -> AIResponse:
        if not self.replies:
            raise AssertionError("AI를 부르면 안 된다")
        return AIResponse(text=self.replies.pop(0), source=Source.REPLAY)


@pytest.fixture(autouse=True)
def scanner(monkeypatch):
    from ddak.plan.patch import pipeline

    monkeypatch.setattr(pipeline, "strict_patch_scan", lambda *_: None)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "snap" / "flaskr").mkdir(parents=True)
    (tmp_path / "snap" / CONFIG).write_text(ORIGINAL, encoding="utf-8")
    return tmp_path


def run(root: Path, provider: FakeProvider, **kw: object) -> PatchConfigOutput:
    inp = PatchConfigInput(run_id="run-1", source_dir="snap", **kw)  # type: ignore[arg-type]
    with tool_context("patch_config", "run-1"):
        return patch_config(inp, ON, root=root, provider=provider, settings=CFG)


def test_proposed_output_is_ready_for_executor_prepare(root: Path) -> None:
    out = run(root, FakeProvider(reply(INTENTS)))
    assert out.status == "proposed" and out.passed and out.patch is not None
    assert out.patch_sha256 == digest_bytes(out.patch.encode("utf-8"))
    assert out.meta is not None and out.meta.reuse is False and out.meta.source is Source.REPLAY
    encode_meta(out.meta.model_dump(mode="json"))  # 실행기 승인 메타 검사를 통과한다
    assert out.targets == {CONFIG: ["local_address", "secret_key"]}
    assert sorted(out.env_vars) == ["APP_BASE_URL", "SECRET_KEY"]
    assert out.attempts == 1 and out.source is Source.REPLAY and out.violations == []
    PatchConfigOutput.model_validate_json(out.model_dump_json())  # JSON 왕복


def test_previous_patch_is_reused_without_ai(root: Path) -> None:
    first = run(root, FakeProvider(reply(INTENTS)))
    assert first.patch is not None
    previous = ApprovedPatch(patch=first.patch, reason="이전 승인", source=Source.LIVE)
    out = run(root, FakeProvider(), previous=previous)
    assert out.status == "reused" and out.passed and out.attempts == 0
    assert out.meta is not None and out.meta.reuse is True and out.source is None


def test_rejected_output_is_not_passed(root: Path) -> None:
    bad = [{**INTENTS[1], "key": "UNAPPROVED_KEY"}]
    out = run(root, FakeProvider(reply(bad), reply(bad)))
    assert out.status == "rejected" and not out.passed and out.meta is None
    assert out.violations and all(v.code for v in out.violations)


def test_empty_intents_are_no_targets_without_retry(root: Path) -> None:
    output = run(root, FakeProvider(reply([])))
    assert output.status == "no_targets" and not output.passed
    assert output.patch is None and output.patch_sha256 is None and output.meta is None
    assert output.env_vars == [] and output.env_keys == [] and output.warnings == []
    assert output.attempts == 1 and output.source is Source.REPLAY
    assert (root / "snap" / CONFIG).read_text() == ORIGINAL


def test_existing_required_keys_do_not_count_as_new_patch_keys(root: Path) -> None:
    from ddak.core.snapshots import apply_diff, copy_source
    from ddak.plan.patch import PatchPreparation

    existing = [f"EXISTING_{i}" for i in range(10)]
    text = "import os\n" + "".join(f'{key} = os.environ["{key}"]\n' for key in existing)
    text += "SESSION_COOKIE_SECURE = False\n"
    (root / "snap" / CONFIG).write_text(text)
    intent = {
        "file": CONFIG,
        "line": 12,
        "pattern_id": "cookie_secure",
        "key": "SESSION_COOKIE_SECURE",
    }
    output = run(root, FakeProvider(reply([intent])))
    assert output.status == "proposed" and output.passed and output.patch
    assert output.env_vars == ["SESSION_COOKIE_SECURE"]
    assert {key.name for key in output.env_keys} == {*existing, "SESSION_COOKIE_SECURE"}
    prepared = PatchPreparation.from_output(output)
    assert prepared.meta["new_env_keys"] == ["SESSION_COOKIE_SECURE"]
    built = root / "built"
    copy_source(root / "snap", built)
    apply_diff(built, output.patch.encode())
    assert (built / CONFIG).read_text() == text.replace(
        "False", "(os.environ['SESSION_COOKIE_SECURE'].lower() == 'true')"
    )
    previous = ApprovedPatch(patch=output.patch, reason="이전 승인", source=Source.REPLAY)
    reused = run(root, FakeProvider(), previous=previous)
    assert reused.status == "reused" and reused.passed and reused.attempts == 0
    assert reused.env_vars == output.env_vars
    assert {(key.name, key.kind, key.required) for key in reused.env_keys} == {
        (key.name, key.kind, key.required) for key in output.env_keys
    }
    assert (root / "snap" / CONFIG).read_text() == text


def test_actual_new_keys_over_contract_limit_reject_without_truncation(root: Path) -> None:
    from dataclasses import replace

    keys = [f"SERVICE_{i}_URL" for i in range(11)]
    text = "".join(f'{key} = "http://localhost:5000"\n' for key in keys)
    (root / "snap" / CONFIG).write_text(text)
    (root / "snap" / "keys.example").write_text("".join(f"{key}=\n" for key in keys))
    intents = [
        {"file": CONFIG, "line": i + 1, "pattern_id": "local_address", "key": key}
        for i, key in enumerate(keys)
    ]
    with tool_context("patch_config", ON.run_id):
        output = patch_config(
            PatchConfigInput(run_id=ON.run_id, source_dir="snap"),
            replace(ON, deploy_config={"env_example": "keys.example"}),
            root=root,
            provider=FakeProvider(reply(intents)),
            settings=CFG,
        )
    assert output.status == "rejected" and not output.passed and output.patch is None
    assert output.env_vars == [] and output.env_keys == [] and output.warnings
    assert (root / "snap" / CONFIG).read_text() == text


@pytest.mark.parametrize("source_dir", ["../snap", "/abs/snap", "missing"])
def test_source_dir_must_be_an_existing_relative_snapshot(root: Path, source_dir: str) -> None:
    inp = PatchConfigInput(run_id="run-1", source_dir=source_dir)
    with pytest.raises(DdakToolError) as caught:
        patch_config(inp, ON, root=root, provider=FakeProvider(), settings=CFG)
    assert caught.value.code is ErrorCode.PRECONDITION_FAILED


def test_patch_config_is_registered() -> None:
    first = load_tools().get("patch_config")
    assert load_tools().get("patch_config") is first
    assert first.fn.__module__ == "ddak.plan.patch.tool"


@pytest.mark.parametrize(
    "text",
    [
        'import os\nAPP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:5000")\n',
        'import os\nDATABASE_URL = os.environ.get(\n    "DATABASE_URL", "mysql://localhost/db"\n)\n',
    ],
)
def test_env_reads_are_not_patch_targets(root, text):
    (root / "snap" / CONFIG).write_text(text)
    out = run(root, FakeProvider())
    assert out.status == "no_targets" and out.patch is None and out.attempts == 0
    assert out.targets == {} and (root / "snap" / CONFIG).read_text() == text


@pytest.mark.parametrize(
    "setting", ["DATABASE_URL", "DB_URL", "SQLALCHEMY_DATABASE_URI", "DB_URI", "DATABASE"]
)
def test_db_access_uses_the_registered_config_pipeline(root, setting, monkeypatch):
    from ddak.core.snapshots import apply_diff, copy_source
    from ddak.plan.patch import generate

    fake_pw = "fake-" + "db-" + "pw-123"
    text = f'{setting} = "mysql+pymysql://app:{fake_pw}@localhost:3306/flaskr"\n'
    (root / "snap" / CONFIG).write_text(text)
    intent = {"file": CONFIG, "line": 1, "pattern_id": "local_address", "key": "DATABASE_URL"}
    requests = []

    class Provider(FakeProvider):
        def complete(self, req):
            requests.append(req)
            return super().complete(req)

    provider = Provider(reply([intent]))
    call_ai = generate.call_ai
    monkeypatch.setattr(
        generate, "call_ai", lambda **kw: call_ai(**{**kw, "provider": provider, "settings": CFG})
    )
    monkeypatch.setenv("DDAK_SOURCES_DIR", str(root))
    inp = PatchConfigInput(run_id="run-1", source_dir="snap")
    with tool_context("patch_config", ON.run_id):
        out = load_tools().get("patch_config").fn(inp, ON)
    assert out.status == "proposed" and out.passed and out.attempts == 1
    assert out.env_vars == ["DATABASE_URL"]
    assert fake_pw not in requests[0].user and "localhost" not in requests[0].user
    assert "os.environ['DATABASE_URL']" in out.patch
    assert out.patch_sha256 == digest_bytes(out.patch.encode())
    built = root / "built"
    copy_source(root / "snap", built)
    apply_diff(built, out.patch.encode())
    assert (built / CONFIG).read_text() == f"import os\n{setting} = os.environ['DATABASE_URL']\n"
    assert (root / "snap" / CONFIG).read_text() == text


def test_app_calls_registry_and_reuses_selected_environment_ledger(root, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from ddak.app import _prepare_config_patch
    from ddak.core.contracts.plan_facts import Facts
    from ddak.core.patch_ledger import save_ledger
    from ddak.core.patch_patterns import scan_patch_targets
    from ddak.core.registry import Registry, spec_for
    from ddak.plan.patch import generate
    from ddak.plan.patch.pipeline import current_patch_session

    provider = FakeProvider(reply(INTENTS))
    call_ai = generate.call_ai
    seen = []

    def generated(**kwargs):
        assert kwargs["settings"] is CFG
        return call_ai(**{**kwargs, "provider": provider})

    monkeypatch.setattr(generate, "call_ai", generated)
    registered = load_tools().get("patch_config")
    registry = Registry([spec_for("patch_config")])

    @registry.tool("patch_config")
    def tracked(inp: PatchConfigInput, ctx: RunContext) -> PatchConfigOutput:
        seen.append(ctx.previous_release)
        return registered.fn(inp, ctx)

    source = root / "snap"
    facts = Facts(
        project="demo",
        mode=ON.mode,
        target="local",
        tiers=("was",),
        changed={"local": {"was": True}},
        code_patch=True,
        patch_targets=scan_patch_targets(source, ()),
        source_snapshot_hash=digest_bytes(b"source"),
        facts_hash=digest_bytes(b"facts"),
    )
    previous = {}
    service = SimpleNamespace(
        registry=registry,
        root=root / "state",
        store=SimpleNamespace(environments=lambda _: previous),
    )
    ctx = replace(ON, project="demo")

    def prepare(current_facts, current_ctx):
        return _prepare_config_patch(
            service,
            source,
            current_facts,
            current_ctx,
            settings=CFG,
            source_root=root,
            selected={"local"},
        )

    first = prepare(facts, ctx)
    assert first.patch and first.meta["passed"] is True
    assert first.meta["patch_sha256"] == digest_bytes(first.patch)
    assert first.changed_files == (CONFIG,)
    assert {key.name for key in first.env_keys} == {"APP_BASE_URL", "SECRET_KEY"}
    run_dir = service.root / "runs" / ctx.run_id
    run_dir.mkdir(parents=True)
    ledger = save_ledger(run_dir, source, first.patch)
    release = {"release_id": ctx.run_id, "patch_ledger": ledger}
    previous.update(
        {"local": {"current": release}, "cloud": {"current": {"patch_ledger": {"invalid": {}}}}}
    )
    off = facts.model_copy(update={"code_patch": False})
    off_ctx = replace(ctx, run_id="run-off", toggles={"code_patch": False})
    reused = prepare(off, off_ctx)
    assert reused.patch == first.patch and reused.meta["reuse"] is True
    assert reused.meta["passed"] is True and reused.meta["source"] == "cache"
    assert seen == [{}, {"local": release}]
    assert provider.replies == []
    assert current_patch_session(off_ctx.run_id) is None
    # 같은 파일이 바뀌었는데 토글이 OFF면 이전 승인 패치를 잃은 채 진행할 수 없다.
    (source / CONFIG).write_text(ORIGINAL + "VERSION = 2\n")
    with pytest.raises(DdakToolError, match="손실"):
        prepare(off, off_ctx)
    assert current_patch_session(off_ctx.run_id) is None


@pytest.mark.parametrize(
    "change",
    [
        {"status": "rejected"},
        {"status": "no_targets"},
        {"status": "patch_lost"},
        {"status": "reused"},
        {"passed": False},
        {"passed": 1},
        {"patch_sha256": None},
        {"patch_sha256": "sha256:" + "0" * 64},
        {"patch": None},
        {"meta": None},
    ],
)
def test_inconsistent_tool_output_cannot_reach_approval(root, change):
    from ddak.plan.patch import PatchPreparation

    output = run(root, FakeProvider(reply(INTENTS)))
    with pytest.raises(DdakToolError, match="패치 툴"):
        PatchPreparation.from_output(output.model_copy(update=change))
    assert (root / "snap" / CONFIG).read_text() == ORIGINAL


@pytest.mark.parametrize(
    "text, intents, expected",
    [
        (
            'import os\napp.config.update(DB_URL="mysql://localhost/db", '
            'SECRET_KEY="dev", DEBUG=os.getenv("DEBUG"))\n',
            [
                {"file": CONFIG, "line": 2, "pattern_id": "local_address", "key": "DATABASE_URL"},
                {"file": CONFIG, "line": 2, "pattern_id": "secret_key", "key": "SECRET_KEY"},
            ],
            "import os\napp.config.update(DB_URL=os.environ['DATABASE_URL'], "
            "SECRET_KEY=os.environ['SECRET_KEY'], DEBUG=os.getenv(\"DEBUG\"))\n",
        ),
        (
            'DB_URL="mysql://localhost/db"\nSQLALCHEMY_DATABASE_URI="mysql://localhost/db"\n',
            [
                {"file": CONFIG, "line": 1, "pattern_id": "local_address", "key": "DATABASE_URL"},
                {"file": CONFIG, "line": 2, "pattern_id": "local_address", "key": "DATABASE_URL"},
            ],
            "import os\nDB_URL=os.environ['DATABASE_URL']\n"
            "SQLALCHEMY_DATABASE_URI=os.environ['DATABASE_URL']\n",
        ),
        (
            'app.config.update(DB_URL="mysql://localhost/db", '
            'SQLALCHEMY_DATABASE_URI="mysql://localhost/db")\n',
            [{"file": CONFIG, "line": 1, "pattern_id": "local_address", "key": "DATABASE_URL"}],
            "import os\napp.config.update(DB_URL=os.environ['DATABASE_URL'], "
            "SQLALCHEMY_DATABASE_URI=os.environ['DATABASE_URL'])\n",
        ),
    ],
)
def test_db_mapping_preserves_independent_settings_and_shared_database_key(
    root, text, intents, expected
):
    from ddak.core.snapshots import apply_diff, copy_source

    (root / "snap" / CONFIG).write_text(text)
    output = run(root, FakeProvider(reply(intents)))
    assert output.status == "proposed" and output.passed and output.attempts == 1
    built = root / "built"
    copy_source(root / "snap", built)
    apply_diff(built, output.patch.encode())
    assert (built / CONFIG).read_text() == expected
    assert output.env_vars == sorted({intent["key"] for intent in intents})
    assert (root / "snap" / CONFIG).read_text() == text


@pytest.mark.parametrize(
    "setting, fallback, pattern, key, converted",
    [
        (
            "SESSION_COOKIE_SECURE=False",
            '{"SESSION_COOKIE_SECURE": False}',
            "cookie_secure",
            "SESSION_COOKIE_SECURE",
            "SESSION_COOKIE_SECURE=(os.environ['SESSION_COOKIE_SECURE'].lower() == 'true')",
        ),
        (
            "MIDDLEWARE=ProxyFix(app, x_for=1)",
            "ProxyFix(app, x_for=2)",
            "proxy_fix",
            "PROXY_FIX_X_FOR",
            "MIDDLEWARE=ProxyFix(app, x_for=int(os.environ['PROXY_FIX_X_FOR']))",
        ),
    ],
)
def test_env_defaults_are_excluded_for_every_supported_pattern(
    root, setting, fallback, pattern, key, converted
):
    from ddak.core.snapshots import apply_diff, copy_source

    text = f'import os\napp.config.update({setting}, AUDIT=os.getenv("AUDIT", {fallback}))\n'
    (root / "snap" / CONFIG).write_text(text)
    intents = [{"file": CONFIG, "line": 2, "pattern_id": pattern, "key": key}]
    output = run(root, FakeProvider(reply(intents)))
    assert output.status == "proposed" and output.passed and output.attempts == 1
    built = root / "built"
    copy_source(root / "snap", built)
    apply_diff(built, output.patch.encode())
    assert (built / CONFIG).read_text() == text.replace(setting, converted, 1)
    assert (root / "snap" / CONFIG).read_text() == text


def test_secret_in_env_default_is_not_an_edit_target_but_still_fails_secret_policy(root):
    from ddak.core.patch_patterns import scan_patch_targets
    from ddak.core.snapshots import apply_diff, copy_source
    from ddak.plan.patch import propose_intents
    from ddak.plan.patch.check import check_patch

    text = (
        'import os\napp.config.update(SECRET_KEY="dev", '
        'AUDIT=os.getenv("AUDIT", {"SECRET_KEY": "audit"}))\n'
    )
    source = root / "snap"
    (source / CONFIG).write_text(text)
    targets = scan_patch_targets(source, ())
    assert len(targets) == 1 and targets[0].key == "SECRET_KEY"
    intents = [{"file": CONFIG, "line": 2, "pattern_id": "secret_key", "key": "SECRET_KEY"}]
    with tool_context("patch_config", ON.run_id):
        patch, keys, _ = propose_intents(
            source, targets, ON, provider=FakeProvider(reply(intents)), settings=CFG
        )
    assert [key.name for key in keys] == ["SECRET_KEY"]
    built = root / "built"
    copy_source(source, built)
    apply_diff(built, patch)
    assert (built / CONFIG).read_text() == text.replace(
        'SECRET_KEY="dev"', "SECRET_KEY=os.environ['SECRET_KEY']", 1
    )
    assert not check_patch(source, patch).passed
    output = run(root, FakeProvider(reply(intents)))
    assert output.status == "rejected" and output.passed is False and output.patch is None
    assert output.attempts == 1 and output.warnings
    assert (source / CONFIG).read_text() == text


@pytest.mark.parametrize(
    "imports, reader",
    [
        ("import os\n", "os.getenv"),
        ("import os\n", "os.environ.get"),
        ("import os\nfrom os import getenv as read\n", "read"),
    ],
)
def test_existing_env_default_does_not_shadow_a_same_line_database_target(root, imports, reader):
    from ddak.core.snapshots import apply_diff, copy_source

    old = 'DB_URL="mysql://localhost/db"'
    other = f'AUDIT_DB={reader}("AUDIT_DB", "mysql://localhost/audit")'
    text = imports + f"app.config.update({old}, {other})\n"
    (root / "snap" / CONFIG).write_text(text)
    intents = [
        {
            "file": CONFIG,
            "line": len(imports.splitlines()) + 1,
            "pattern_id": "local_address",
            "key": "DATABASE_URL",
        }
    ]
    output = run(root, FakeProvider(reply(intents)))
    assert output.status == "proposed" and output.passed and output.attempts == 1
    built = root / "built"
    copy_source(root / "snap", built)
    apply_diff(built, output.patch.encode())
    expected = text.replace(old, "DB_URL=os.environ['DATABASE_URL']")
    assert (built / CONFIG).read_text() == expected
    assert (root / "snap" / CONFIG).read_text() == text


def test_patch_lost_output_is_not_passed_and_names_the_file(root: Path) -> None:
    first = run(root, FakeProvider(reply(INTENTS)))
    assert first.patch is not None
    (root / "snap" / CONFIG).write_text(ORIGINAL + "# v3\n", encoding="utf-8")  # 원본이 바뀜
    inp = PatchConfigInput(
        run_id="run-1",
        source_dir="snap",
        previous=ApprovedPatch(patch=first.patch, reason="이전 승인"),
    )
    with tool_context("patch_config", "run-1"):
        out = patch_config(inp, RunContext("run-1"), root=root, provider=FakeProvider())  # 토글 OFF
    assert out.status == "patch_lost" and out.passed is False and out.patch is None
    assert [(v.code, v.file, v.line) for v in out.violations] == [
        ("patch_lost", CONFIG, 6),
        ("patch_lost", CONFIG, 7),
    ]
    assert out.patch_sha256 is None and out.meta is None and out.attempts == 0
    assert (root / "snap" / CONFIG).read_text() == ORIGINAL + "# v3\n"
