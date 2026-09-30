"""승인 전 미리보기와 승인 후 빌드 사본의 실제 파일 결합을 확인한다."""

from __future__ import annotations

import difflib
import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

from ddak.core.snapshots import copy_source, digest_json, file_manifest, materialize, preview

PATCH = b"--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-VERSION = 1\n+VERSION = 2\n"


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    (root / "app.py").write_bytes(b"VERSION = 1\n")
    return root


def test_copy_preserves_content_and_executable_hash_and_excludes_runtime_files(
    source: Path, tmp_path: Path
) -> None:
    script = source / "start.sh"
    script.write_bytes(b"exit 0\n")
    script.chmod(0o755)
    for name in (".env", ".env.local", ".git/config", ".secrets/value", "var/log", ".venv/x"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("excluded fixture")
    destination = tmp_path / "copy"
    manifest = copy_source(source, destination)
    assert set(manifest) == {"app.py", "start.sh"}
    assert manifest["app.py"] == {
        "sha256": "sha256:" + hashlib.sha256(b"VERSION = 1\n").hexdigest(),
        "executable": False,
    }
    assert manifest["start.sh"]["executable"] is True
    assert (destination / "start.sh").stat().st_mode & stat.S_IXUSR
    assert file_manifest(destination) == manifest
    assert {p.name for p in destination.iterdir()} == {"app.py", "start.sh"}
    assert preview(source).source_snapshot_hash == digest_json(manifest)


def test_exact_patch_binds_preview_to_materialized_copy(source: Path, tmp_path: Path) -> None:
    binding = preview(source, PATCH)
    assert (source / "app.py").read_bytes() == b"VERSION = 1\n"
    assert binding.patch_sha256 == "sha256:" + hashlib.sha256(PATCH).hexdigest()
    assert binding.source_snapshot_hash != binding.build_snapshot_hash
    destination = tmp_path / "build"
    materialize(source, destination, binding, PATCH)
    assert (destination / "app.py").read_bytes() == b"VERSION = 2\n"
    assert digest_json(file_manifest(destination)) == binding.build_snapshot_hash
    with pytest.raises(ValueError, match="diff 해시"):
        materialize(source, tmp_path / "different-patch", binding, PATCH.replace(b"= 2", b"= 3"))
    assert not (tmp_path / "different-patch").exists()


def test_materialize_refuses_source_changed_after_approval(source: Path, tmp_path: Path) -> None:
    binding = preview(source)
    (source / "app.py").write_bytes(b"VERSION = 9\n")
    with pytest.raises(ValueError, match="원본 소스가 변경"):
        materialize(source, tmp_path / "build", binding, None)


def test_source_symlink_and_patch_parent_path_are_rejected(source: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"VERSION = 1\n")
    (source / "linked.py").symlink_to(outside)
    with pytest.raises(ValueError, match="심볼릭 링크"):
        preview(source)
    (source / "linked.py").unlink()
    with pytest.raises(ValueError, match="허용되지 않는 경로"):
        preview(source, PATCH.replace(b"app.py", b"../outside.py"))
    assert outside.read_bytes() == b"VERSION = 1\n"
    assert (source / "app.py").read_bytes() == b"VERSION = 1\n"


def test_databases_pem_files_and_caches_do_not_enter_snapshot_or_build_copy(
    source: Path, tmp_path: Path
) -> None:
    expected_manifest = file_manifest(source)
    expected_binding = preview(source)
    for name in (
        "instance/db.sqlite",
        "foo.sqlite",
        "nested/foo.sqlite3",
        "certificates/server.pem",
        "certificates/server.key",
        ".pytest_cache/state",
        ".ruff_cache/state",
        ".mypy_cache/state",
        ".cache/state",
        "package/__pycache__/module.pyc",
        "node_modules/package/index.js",
        ".DS_Store",
    ):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"excluded runtime fixture\n")
    assert file_manifest(source) == expected_manifest
    assert preview(source) == expected_binding
    destination = tmp_path / "build"
    materialize(source, destination, expected_binding, None)
    assert {path.relative_to(destination).as_posix() for path in destination.rglob("*")} == {
        "app.py"
    }
    assert file_manifest(destination) == expected_manifest


def test_exact_patch_is_applied_inside_parent_git_repository(source: Path, tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        check=True,
        capture_output=True,
        timeout=10,
        env={
            "PATH": os.environ.get("PATH", os.defpath),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    (repository / "app.py").write_bytes(b"VERSION = 42\n")
    build_root = repository / "var" / "run" / "build"
    binding = preview(source, PATCH)
    materialize(source, build_root, binding, PATCH)
    # 종료 코드 0만으로는 부족하다. 부모 Git prefix 때문에 패치가 무시되지 않아야 한다.
    assert (build_root / "app.py").read_bytes() == b"VERSION = 2\n"
    assert digest_json(file_manifest(build_root)) == binding.build_snapshot_hash
    assert (source / "app.py").read_bytes() == b"VERSION = 1\n"
    assert (repository / "app.py").read_bytes() == b"VERSION = 42\n"
    assert (repository / ".git").is_dir()
    assert not (build_root / ".git").exists()


@pytest.mark.parametrize(
    "before,after",
    [
        ("TIMEOUT_MS = 120000\n", "TIMEOUT_MS = 240000\n"),
        ("-- legacy\n", "-- updated\n"),
        ("++ legacy\n", "++ updated\n"),
        ("rename from old\n", "rename from new\n"),
    ],
)
def test_patch_body_is_not_interpreted_as_file_headers_or_modes(
    source: Path, tmp_path: Path, before: str, after: str
) -> None:
    (source / "app.py").write_text(before)
    patch = "".join(
        difflib.unified_diff(
            before.splitlines(True), after.splitlines(True), "a/app.py", "b/app.py"
        )
    ).encode()
    binding = preview(source, patch)
    materialize(source, tmp_path / "build", binding, patch)
    assert (tmp_path / "build" / "app.py").read_text() == after
    assert (source / "app.py").read_text() == before


def test_symlink_mode_is_still_rejected(source: Path) -> None:
    patch = (
        b"diff --git a/link b/link\nnew file mode 120000\n"
        b"--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n+app.py\n"
    )
    with pytest.raises(ValueError, match="일반 텍스트"):
        preview(source, patch)


@pytest.mark.parametrize("separator", ["\f", "\u2028"])
def test_patch_counts_only_lf_as_hunk_line_separator(
    source: Path, tmp_path: Path, separator: str
) -> None:
    before, after = f"old{separator}value\n", f"new{separator}value\n"
    (source / "app.py").write_text(before)
    patch = ("--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-" + before + "+" + after).encode()
    binding = preview(source, patch)
    materialize(source, tmp_path / "build", binding, patch)
    assert (tmp_path / "build" / "app.py").read_text() == after
