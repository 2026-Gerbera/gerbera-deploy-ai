"""source=fixture: 승인 diff 무결성·정확 해시 재사용·이월 관문."""

import json
from copy import deepcopy

import pytest

from ddak.core.contracts.errors import DdakToolError
from ddak.core.patch_ledger import approved_patches, file_diff, reuse_patches, save_ledger
from ddak.core.snapshots import apply_diff, digest_bytes
from ddak.executor.images import guard_carried_trees, tier_tree_hashes


@pytest.fixture
def history(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    old = b'import os\nSECRET_KEY = "' + b'dev"\n'
    fixed = b'import os\nSECRET_KEY = os.environ["SECRET_KEY"]\n'
    (source / "app.py").write_bytes(old)
    run = tmp_path / "runs" / "run-first"
    run.mkdir(parents=True)
    patch = file_diff("app.py", old, fixed)
    entries = save_ledger(run, source, patch)
    return (
        source,
        tmp_path / "runs",
        {"local": {"release_id": "run-first", "patch_ledger": entries}},
        patch,
    )


def test_reuse_ignores_unrelated_source_changes_and_checks_private_diff(history):
    source, runs, previous, patch = history
    (source / "unrelated.py").write_text("VERSION = 2\n")
    assert reuse_patches(source, previous, runs) == (patch, [])
    stored = next((runs / "run-first" / "patches").iterdir())
    assert stored.stat().st_mode & 0o777 == 0o600
    stored.write_bytes(patch + b"\n")
    with pytest.raises(DdakToolError, match="원장"):
        reuse_patches(source, previous, runs)


def test_changed_original_requires_tool_review_without_core_content_judgment(history):
    source, runs, previous, patch = history
    (source / "app.py").write_bytes((source / "app.py").read_bytes() + b"VERSION = 2\n")
    assert approved_patches(previous, runs) == {"app.py": patch}
    assert reuse_patches(source, previous, runs) == (None, ["app.py"])


def test_upstream_exact_fix_is_changed_source_for_exact_hash_reuse(history):
    source, runs, previous, patch = history
    apply_diff(source, patch)
    assert reuse_patches(source, previous, runs) == (None, ["app.py"])
    assert approved_patches(previous, runs) == {"app.py": patch}
    assert "dev" not in json.dumps(previous)


def test_deleted_source_is_review_candidate_without_core_loss_verdict(history):
    source, runs, previous, patch = history
    (source / "app.py").unlink()
    assert approved_patches(previous, runs) == {"app.py": patch}
    assert reuse_patches(source, previous, runs) == (None, ["app.py"])


def test_exact_reuse_checks_result_hash(history):
    source, runs, previous, patch = history
    previous["local"]["patch_ledger"]["app.py"]["result_sha256"] = digest_bytes(b"other result")
    assert approved_patches(previous, runs) == {"app.py": patch}
    with pytest.raises(DdakToolError, match="원장"):
        reuse_patches(source, previous, runs)


@pytest.mark.parametrize("missing", [False, True])
def test_private_diff_integrity_is_checked_even_when_source_changed(history, missing):
    source, runs, previous, patch = history
    (source / "app.py").write_text("VERSION = 2\n")
    stored = next((runs / "run-first" / "patches").iterdir())
    if missing:
        stored.unlink()
    else:
        stored.write_bytes(patch + b"\n")
    with pytest.raises(DdakToolError, match="원장"):
        approved_patches(previous, runs)
    with pytest.raises(DdakToolError, match="원장"):
        reuse_patches(source, previous, runs)


def test_matching_dual_environment_ledgers_deduplicate_patch(history):
    source, runs, previous, patch = history
    previous["cloud"] = deepcopy(previous["local"])
    assert approved_patches(previous, runs) == {"app.py": patch}
    assert reuse_patches(source, previous, runs) == (patch, [])


@pytest.mark.parametrize("field", ["source_sha256", "result_sha256", "patch_sha256"])
def test_dual_environment_conflict_is_rejected_even_when_source_changed(history, field):
    source, runs, previous, patch = history
    previous["cloud"] = deepcopy(previous["local"])
    entry = previous["cloud"]["patch_ledger"]["app.py"]
    if field == "patch_sha256":
        other = patch + b"\n"
        entry[field] = digest_bytes(other)
        stored = runs / "run-first" / "patches" / (entry[field][7:] + ".diff")
        stored.write_bytes(other)
    else:
        entry[field] = digest_bytes(b"different approved tree")
    (source / "app.py").write_text("VERSION = 2\n")
    with pytest.raises(DdakToolError, match="원장"):
        approved_patches(previous, runs)
    with pytest.raises(DdakToolError, match="원장"):
        reuse_patches(source, previous, runs)


def test_carried_tier_requires_its_original_build_tree():
    cfg = {"tiers": {"web": {"paths": ["web", "shared"], "dockerfile": "docker/web"}}}
    files = {"web/a": {"sha256": "one"}, "shared/b": {"sha256": "two"}}
    hashes = tier_tree_hashes(files, cfg)
    previous = {"local": {"image_sources": {"web": {"tier_tree_hash": hashes["web"]}}}}
    guard_carried_trees({"local": {"web": "digest"}}, previous, hashes)
    changed = tier_tree_hashes({**files, "shared/b": {"sha256": "new"}}, cfg)
    with pytest.raises(DdakToolError, match="재빌드"):
        guard_carried_trees({"local": {"web": "digest"}}, previous, changed)
    with pytest.raises(DdakToolError):
        guard_carried_trees({"local": {"web": "digest"}}, {"local": {}}, hashes)


def test_dot_prefix_and_path_spelling_cannot_hide_carried_changes():
    cfg = {"tiers": {"was": {"paths": ["./was/"], "dockerfile": "./docker/was"}}}
    first = {"was/app.py": {"sha256": "one"}, "docker/was": {"sha256": "docker"}}
    changed = {**first, "was/app.py": {"sha256": "two"}}
    assert tier_tree_hashes(first, cfg) != tier_tree_hashes(changed, cfg)
