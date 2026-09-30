"""O1 빌드 사본. O2는 같은 file manifest 규칙을 변경 탐지에 사용할 수 있다."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ddak.core.contracts.release import SnapshotBinding

EXCLUDED = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".secrets",
    "var",
    "instance",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".cache",
    ".DS_Store",
    ".aws",
    ".ssh",
    ".claude",
    ".codex",
}


def excluded(path: Path) -> bool:
    return any(p in EXCLUDED or p.startswith(".env") for p in path.parts) or path.suffix in {
        ".sqlite",
        ".sqlite3",
        ".pem",
        ".key",
    }


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_json(data: Any) -> str:
    return digest_bytes(
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )


def file_manifest(root: Path) -> dict[str, dict[str, Any]]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("소스는 실제 디렉토리여야 한다")
    files: dict[str, dict[str, Any]] = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        dirs[:] = sorted(d for d in dirs if not excluded((parent / d).relative_to(root)))
        for name in [*dirs, *sorted(names)]:
            path = parent / name
            relative = path.relative_to(root)
            if excluded(relative):
                continue
            if path.is_symlink():
                raise ValueError("소스 심볼릭 링크는 지원하지 않는다")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError("소스에 일반 파일이 아닌 항목이 있다")
            files[relative.as_posix()] = {
                "sha256": digest_bytes(path.read_bytes()),
                "executable": bool(path.stat().st_mode & stat.S_IXUSR),
            }
    return files


def copy_source(source: Path, dest: Path) -> dict[str, dict[str, Any]]:
    manifest = file_manifest(source)
    dest.mkdir(parents=True, exist_ok=False)
    for name in manifest:
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    if file_manifest(dest) != manifest or file_manifest(source) != manifest:
        raise ValueError("복사 중 소스가 변경됐다")
    return manifest


def apply_diff(root: Path, patch: bytes) -> None:
    # git apply는 경로 탈출을 거부한다. 링크/바이너리/제외 파일 수정도 허용하지 않는다.
    text = patch.decode("utf-8")
    old_left = new_left = 0
    for line in text.split("\n"):
        # hunk 본문의 SQL 주석(--- note)이나 숫자 120000은 헤더/파일 모드가 아니다.
        if old_left or new_left:
            if line.startswith("\\ No newline at end of file"):
                continue
            if not line or line[0] not in " +-":
                raise ValueError("패치 hunk 형식이 잘못됐다")
            old_left -= line[0] in " -"
            new_left -= line[0] in " +"
            if old_left < 0 or new_left < 0:
                raise ValueError("패치 hunk 줄 수가 다르다")
            continue
        if line.startswith("@@ "):
            hunk = re.match(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
            if not hunk:
                raise ValueError("패치 hunk 형식이 잘못됐다")
            old_left, new_left = (int(count) if count is not None else 1 for count in hunk.groups())
            continue
        if line.startswith(
            ("GIT binary patch", "rename from ", "rename to ", "copy from ", "copy to ")
        ) or (
            line.startswith(
                ("new file mode ", "deleted file mode ", "old mode ", "new mode ", "index ")
            )
            and re.search(r"\b120000\b", line)
        ):
            raise ValueError("패치는 일반 텍스트 파일 수정만 지원한다")
        if line.startswith(("--- ", "+++ ")):
            name = line[4:].split("\t", 1)[0]
            if name == "/dev/null":
                continue
            if not name.startswith(("a/", "b/")):
                raise ValueError("패치 경로는 a/ 또는 b/ 접두사가 필요하다")
            parts = Path(name[2:]).parts
            if not parts or ".." in parts or excluded(Path(name[2:])):
                raise ValueError("패치에 허용되지 않는 경로가 있다")
    if old_left or new_left:
        raise ValueError("패치 hunk 줄 수가 다르다")
    for check in (True, False):
        args = ["git", "apply", "--no-index", "--whitespace=nowarn"]
        if check:
            args.append("--check")
        result = subprocess.run(
            [*args, "-"],
            cwd=root,
            input=patch,
            capture_output=True,
            timeout=10,
            check=False,
            env={
                "PATH": os.environ.get("PATH", os.defpath),
                "GIT_CEILING_DIRECTORIES": str(root.resolve().parent),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
        if result.returncode:
            raise ValueError("패치를 빌드 사본에 정확히 적용할 수 없다")
    file_manifest(root)  # 패치가 링크/특수 파일을 만들었다면 거부


def preview(source: Path, patch: bytes | None = None) -> SnapshotBinding:
    with tempfile.TemporaryDirectory(prefix="ddak-preview-") as temp:
        root = Path(temp) / "source"
        original = copy_source(source, root)
        if patch:
            apply_diff(root, patch)
        return SnapshotBinding(
            source_snapshot_hash=digest_json(original),
            build_snapshot_hash=digest_json(file_manifest(root)),
            patch_sha256=digest_bytes(patch) if patch else None,
        )


def materialize(source: Path, dest: Path, expected: SnapshotBinding, patch: bytes | None) -> None:
    if (digest_bytes(patch) if patch else None) != expected.patch_sha256:
        raise ValueError("승인된 diff 해시와 다르다")
    original = copy_source(source, dest)
    if digest_json(original) != expected.source_snapshot_hash:
        raise ValueError("승인 뒤 원본 소스가 변경됐다")
    if patch:
        apply_diff(dest, patch)
    if digest_json(file_manifest(dest)) != expected.build_snapshot_hash:
        raise ValueError("승인된 수정본 해시와 다르다")
