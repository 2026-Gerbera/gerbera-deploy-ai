"""source=fixture: 결정 12의 파일 단위 재사용·손실 판정은 제품 툴 준비 경로에서 한다."""

import pytest

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.patch_ledger import file_diff, save_ledger
from ddak.plan.patch.pipeline import prepare_patch


@pytest.fixture
def previous(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    old = {
        "app.py": 'import os\nSECRET_KEY = "' + 'dev"\n',
        "settings.py": 'import os\nHOST = "localhost"\n',
    }
    fixed = {
        "app.py": 'import os\nSECRET_KEY = os.environ["SECRET_KEY"]\n',
        "settings.py": 'import os\nHOST = os.environ["HOST"]\n',
    }
    for name, content in old.items():
        (source / name).write_text(content)
    return source, tmp_path / "runs", old, fixed


def record(source, runs, old, fixed):
    directory = runs / "first"
    directory.mkdir(parents=True)
    patch = b"".join(file_diff(name, old[name].encode(), fixed[name].encode()) for name in old)
    entries = save_ledger(directory, source, patch)
    return {"local": {"release_id": "first", "patch_ledger": entries}}


def prepare(source, runs, history, *, enabled=False, proposer=None):
    return prepare_patch(
        source,
        None,
        RunContext("run-d12", toggles={"code_patch": enabled}),
        previous=history,
        runs_root=runs,
        proposer=proposer,
        scanner=lambda *_: None,
    )


def test_off_unchanged_reuses_all_files_without_ai(previous):
    source, runs, old, fixed = previous
    history = record(source, runs, old, fixed)

    def no_ai(*_):
        pytest.fail("OFF must not call AI")

    out = prepare(source, runs, history, proposer=no_ai)
    assert out.meta["reuse"] is True and out.violations == ()
    assert set(out.changed_files) == set(old)


@pytest.mark.parametrize("enabled", [False, True])
def test_optional_legacy_file_alone_is_reproposed_or_lost(previous, enabled):
    source, runs, old, fixed = previous
    legacy = {**fixed, "settings.py": 'import os\nHOST = os.getenv("HOST", "localhost")\n'}
    history = record(source, runs, old, legacy)
    calls = []

    def propose(root, targets, ctx):
        calls.append({t.file for t in targets})
        return (
            file_diff("settings.py", old["settings.py"].encode(), fixed["settings.py"].encode()),
            (),
            "fixture",
        )

    out = prepare(source, runs, history, enabled=enabled, proposer=propose)
    if enabled:
        assert calls == [{"settings.py"}]
        assert out.violations == () and out.meta["reuse"] is False
        assert set(out.changed_files) == set(old)
        assert b"getenv" not in out.patch
    else:
        assert calls == [] and out.patch is None
        assert [(v.code, v.file, v.line) for v in out.violations] == [
            ("patch_lost", "settings.py", 2)
        ]


def test_on_changed_file_only_goes_to_ai(previous):
    source, runs, old, fixed = previous
    history = record(source, runs, old, fixed)
    (source / "app.py").write_text("VERSION = 2\n" + old["app.py"])
    calls = []

    def propose(root, targets, ctx):
        calls.append({t.file for t in targets})
        return (
            file_diff(
                "app.py",
                (root / "app.py").read_bytes(),
                ("VERSION = 2\n" + fixed["app.py"]).encode(),
            ),
            (),
            "fixture",
        )

    out = prepare(source, runs, history, enabled=True, proposer=propose)
    assert calls == [{"app.py"}] and out.violations == ()
    assert set(out.changed_files) == set(old)


@pytest.mark.parametrize("failure", ["empty", "invalid", "unavailable"])
def test_ai_failure_never_drops_loss_gate(previous, failure):
    source, runs, old, fixed = previous
    history = record(source, runs, old, fixed)
    (source / "app.py").write_text("VERSION = 2\n" + old["app.py"])

    def propose(*_):
        if failure == "unavailable":
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "fixture")
        return (b"" if failure == "empty" else b"invalid"), (), "fixture"

    out = prepare(source, runs, history, enabled=True, proposer=propose)
    assert out.patch is None
    assert [(v.file, v.line) for v in out.violations] == [("app.py", 3)]
    assert "dev" not in str(out.violations)


@pytest.mark.parametrize("change", ["removed-line", "removed-file", "required-read"])
def test_developer_direct_change_is_not_loss(previous, change):
    source, runs, old, fixed = previous
    history = record(source, runs, old, fixed)
    target = source / "app.py"
    if change == "removed-file":
        target.unlink()
    else:
        target.write_text("VERSION = 2\n" + (fixed["app.py"] if change == "required-read" else ""))
    out = prepare(source, runs, history)
    assert out.violations == ()
    assert out.changed_files == ("settings.py",)


def test_policy_rejection_cannot_discard_previous_patch_after_loss_check(previous):
    from ddak.plan.patch.check import PatchPolicy

    source, runs, old, fixed = previous
    history = record(source, runs, old, fixed)
    out = prepare_patch(
        source,
        None,
        RunContext("run-d12", toggles={"code_patch": False}),
        previous=history,
        runs_root=runs,
        scanner=lambda *_: None,
        policy=PatchPolicy(allowed_files=frozenset({"settings.py"})),
    )
    assert out.patch is None
    assert [(v.file, v.line) for v in out.violations] == [("app.py", 2)]
