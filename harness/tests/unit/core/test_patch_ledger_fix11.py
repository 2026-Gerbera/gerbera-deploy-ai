"""source=fixture: 파일별 재사용·손실·이월 관문."""

import json

import pytest

from ddak.core.contracts.errors import DdakToolError
from ddak.core.patch_ledger import file_diff, guard_patch_loss, reuse_patches, save_ledger
from ddak.core.snapshots import apply_diff, copy_source
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


def test_changed_original_is_reproposal_and_off_cannot_drop_patch(history, tmp_path):
    source, runs, previous, _ = history
    (source / "app.py").write_bytes((source / "app.py").read_bytes() + b"VERSION = 2\n")
    assert reuse_patches(source, previous, runs) == (None, ["app.py"])
    built = tmp_path / "build"
    copy_source(source, built)
    with pytest.raises(DdakToolError, match="손실"):
        guard_patch_loss(source, built, previous)


def test_upstream_exact_fix_is_not_patch_loss(history, tmp_path):
    source, _, previous, patch = history
    apply_diff(source, patch)
    built = tmp_path / "build"
    copy_source(source, built)
    guard_patch_loss(source, built, previous)
    assert "dev" not in json.dumps(previous)


def test_unchanged_source_requires_exact_patched_result(history, tmp_path):
    source, _, previous, patch = history
    built = tmp_path / "build"
    copy_source(source, built)
    with pytest.raises(DdakToolError):
        guard_patch_loss(source, built, previous)
    apply_diff(built, patch)
    guard_patch_loss(source, built, previous)


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
