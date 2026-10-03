"""승인 패치의 기록·무결성·재사용. 내용 판정은 patch_config 결과를 따른다."""

from __future__ import annotations

import ast
import difflib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.patch_config import PatchConfigOutput
from ddak.core.snapshots import apply_diff, copy_source, digest_bytes, digest_json, file_manifest


def _fail(path: str) -> DdakToolError:
    return DdakToolError(
        ErrorCode.PRECONDITION_FAILED, f"패치 원장 불일치/손실: {path}; 재제안 필요"
    )


def file_diff(path: str, old: bytes, new: bytes) -> bytes:
    """원본 바이트로 렌더한다. 파일당 하나의 hunk, 줄바꿈 없음 표시 포함."""
    before = re.findall(r"[^\n]*\n|[^\n]+$", old.decode())
    after = re.findall(r"[^\n]*\n|[^\n]+$", new.decode())
    out = []
    for line in difflib.unified_diff(
        before, after, fromfile=f"a/{path}", tofile=f"b/{path}", n=max(len(before), len(after))
    ):
        out.append(line)
        if not line.endswith("\n"):
            out.append("\n\\ No newline at end of file\n")
    return "".join(out).encode()


def _semantic_sites(data: bytes) -> tuple[dict[str, list[str]], set[str]]:
    """줄 위치 대신 lexical owner/설정 대상과 표현식을 해시한다. 원문은 반환하지 않는다."""
    try:
        tree = ast.parse(data)
    except (SyntaxError, ValueError):
        return {}, set()
    sites: dict[str, list[str]] = {}
    required: set[str] = set()
    protected = {"os"}
    converters = {"int", "bool", "str", "float"}

    def dump(node: ast.AST) -> str:
        return ast.dump(node, include_attributes=False)

    def add(owner: tuple[str, ...], target: str, value: ast.AST) -> None:
        identity = digest_json([*owner, target])
        sites.setdefault(identity, []).append(digest_bytes(dump(value).encode()))
        nodes = list(ast.walk(value))
        if any(
            isinstance(n, ast.Subscript)
            and isinstance(n.ctx, ast.Load)
            and isinstance(n.value, ast.Attribute)
            and isinstance(n.value.value, ast.Name)
            and n.value.value.id == "os"
            and n.value.attr == "environ"
            and isinstance(n.slice, ast.Constant)
            and isinstance(n.slice.value, str)
            for n in nodes
        ):
            required.add(identity)
            protected.update(
                n.func.id
                for n in nodes
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id in converters
            )

    def visit(node: ast.AST, owner: tuple[str, ...] = ()) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            owner += (type(node).__name__, node.name)
        elif isinstance(node, ast.If | ast.While):
            owner += (type(node).__name__, dump(node.test))
        elif isinstance(node, ast.For | ast.AsyncFor):
            owner += (type(node).__name__, dump(node.target), dump(node.iter))
        elif isinstance(node, ast.With | ast.AsyncWith):
            owner += (type(node).__name__, *(dump(item) for item in node.items))
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            target = "assignment:" + ",".join(dump(t) for t in targets)
            # from_mapping/ProxyFix의 각 keyword는 독립적인 의무다.
            if node.value is not None and not (
                isinstance(node.value, ast.Dict)
                or (isinstance(node.value, ast.Call) and node.value.keywords)
            ):
                add(owner, target, node.value)
            owner += (target,)
        elif isinstance(node, ast.Call):
            owner += ("call:" + dump(node.func),)
            for index, arg in enumerate(node.args):
                add(owner, f"argument:{index}", arg)
            for keyword in node.keywords:
                add(owner, "keyword:" + str(keyword.arg), keyword.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                add(owner, "dict:" + (dump(key) if key is not None else "**"), value)
        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                # then/else, try/except를 같은 owner로 취급하지 않는다.
                branch = (
                    (*owner, field)
                    if field in {"body", "orelse", "finalbody", "handlers"}
                    else owner
                )
                for child in value:
                    if isinstance(child, ast.AST):
                        visit(child, branch)
            elif isinstance(value, ast.AST):
                visit(value, owner)

    visit(tree)
    if not required:
        return sites, required
    # 렌더러의 정식 module os import와 builtin 형 변환만 의미 증거로 인정한다.
    imported = False
    for node in ast.walk(tree):
        names: set[str] = set()
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                if name == "os" and alias.name == "os" and node in tree.body:
                    imported = True
                else:
                    names.add(name)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) or (
            isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and node.name
        ):
            if node.name is not None:
                names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store | ast.Del)
            and any(isinstance(n, ast.Name) and n.id == "os" for n in ast.walk(node))
        ):
            names.add("os")
        if "*" in names or names & protected:
            return {}, set()
    return (
        ({key: sorted(values) for key, values in sites.items()}, required)
        if imported
        else ({}, set())
    )


def ledger(source: Path, built: Path) -> tuple[dict[str, dict[str, Any]], dict[str, bytes]]:
    original, result = file_manifest(source), file_manifest(built)
    if original.keys() != result.keys():
        raise _fail("파일 목록")
    entries, patches = {}, {}
    for name in original:
        if original[name] == result[name]:
            continue
        old, new = (source / name).read_bytes(), (built / name).read_bytes()
        patch = file_diff(name, old, new)
        removed = []
        for line in patch.splitlines():
            if line.startswith(b"-") and not line.startswith(b"---") and line[1:].strip():
                removed.append(digest_bytes(line[1:].strip()))
        sites, required = _semantic_sites(new) if name.endswith(".py") else ({}, set())
        before_sites = _semantic_sites(old)[0] if name.endswith(".py") else {}
        entries[name] = {
            "source_sha256": original[name]["sha256"],
            "result_sha256": result[name]["sha256"],
            "patch_sha256": digest_bytes(patch),
            "removed_lines": sorted(set(removed)),
            "required_env_expressions": {
                key: sites[key] for key in sorted(required) if before_sites.get(key) != sites[key]
            },
        }
        patches[name] = patch
    return entries, patches


def save_ledger(directory: Path, source: Path, patch: bytes | None) -> dict[str, dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="ddak-ledger-") as tmp:
        built = Path(tmp) / "tree"
        copy_source(source, built)
        if patch:
            apply_diff(built, patch)
        entries, patches = ledger(source, built)
    private = directory / "patches"
    private.mkdir(mode=0o700, exist_ok=True)
    for name, data in patches.items():
        path = private / (entries[name]["patch_sha256"].removeprefix("sha256:") + ".diff")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    return entries


def approved_patches(
    previous: Mapping[str, Mapping[str, Any]], runs_root: Path
) -> dict[str, bytes]:
    """변경 파일도 포함해 승인 diff를 읽는다. 경로·해시·환경 간 일치만 검사한다."""
    patches: dict[str, bytes] = {}
    entries: dict[str, tuple[str, str]] = {}
    for release in previous.values():
        rid = release.get("release_id")
        if not isinstance(rid, str) or Path(rid).name != rid or rid in (".", ".."):
            if release.get("patch_ledger"):
                raise _fail("release_id")
            continue
        for name, entry in release.get("patch_ledger", {}).items():
            pure = Path(name)
            if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != name:
                raise _fail("파일 경로")
            hashes = tuple(
                entry.get(key) for key in ("source_sha256", "result_sha256", "patch_sha256")
            )
            if any(
                not isinstance(h, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", h)
                for h in hashes
            ):
                raise _fail(name)
            source_hash, result_hash, digest = hashes
            path = runs_root / rid / "patches" / (digest[7:] + ".diff")
            if (
                any(parent.is_symlink() for parent in (path, path.parent, path.parent.parent))
                or not path.is_file()
                or path.stat().st_size > 65536
            ):
                raise _fail(name)
            data = path.read_bytes()
            identity = (source_hash, result_hash)
            if digest_bytes(data) != digest or (
                name in patches and (patches[name] != data or entries[name] != identity)
            ):
                raise _fail(name)
            patches[name], entries[name] = data, identity
    return patches


def reuse_patches(
    source: Path, previous: Mapping[str, Mapping[str, Any]], runs_root: Path
) -> tuple[bytes | None, list[str]]:
    """승인 원본과 결과의 정확한 해시 일치. 패치의 내용·손실 여부는 판단하지 않는다."""
    manifest = file_manifest(source)
    approved = approved_patches(previous, runs_root)
    patches: dict[str, bytes] = {}
    changed: set[str] = set()
    for release in previous.values():
        for name, entry in release.get("patch_ledger", {}).items():
            if manifest.get(name, {}).get("sha256") != entry["source_sha256"]:
                changed.add(name)
                continue
            data = approved[name]
            with tempfile.TemporaryDirectory(prefix="ddak-reuse-") as tmp:
                built = Path(tmp) / "source"
                before = copy_source(source, built)
                apply_diff(built, data)
                after = file_manifest(built)
                if (
                    after.get(name, {}).get("sha256") != entry["result_sha256"]
                    or before.keys() != after.keys()
                    or any(before[n] != after[n] for n in before if n != name)
                ):
                    raise _fail(name)
            patches[name] = data
    return b"".join(patches[k] for k in sorted(patches)) or None, sorted(changed)


class PatchLostError(DdakToolError):
    """툴의 손실 판정을 위치만 담아 전달한다. 내용은 다시 판정하지 않는다."""

    def __init__(self, review: PatchConfigOutput) -> None:
        self.locations = tuple({"file": v.file, "line": v.line} for v in review.violations[:50])
        locations = (
            ", ".join(
                f"{v.file}:{v.line}" if v.line else (v.file or "이전 패치")
                for v in review.violations[:50]
            )
            or "이전 패치"
        )
        super().__init__(
            ErrorCode.PRECONDITION_FAILED,
            f"패치 툴 손실(patch_lost): {locations} (값 가림); 재제안 필요",
        )


def guard_patch_loss(
    review: PatchConfigOutput | None,
    *,
    run_id: str,
    patch: bytes | None,
    has_previous: bool = False,
) -> None:
    """내용을 다시 판정하지 않고 툴 결과의 중단·승인 연결만 강제한다."""
    if review is None:
        if has_previous:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "이전 패치의 툴 판정이 필요하다")
        return
    if review.run_id != run_id:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "패치 툴 판정의 run ID가 다르다")
    if review.status == "patch_lost":
        raise PatchLostError(review)
    accepted = review.status in {"proposed", "reused"}
    if (
        accepted
        and (
            review.passed is not True
            or not patch
            or review.patch_sha256 != digest_bytes(patch)
            or review.patch != patch.decode("utf-8")
        )
    ) or (not accepted and (review.passed or review.patch is not None or patch is not None)):
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "패치 툴 판정과 승인 대상이 다르다")


def read_ledger(directory: Path) -> dict[str, Any]:
    path = directory / "patch-ledger.json"
    return json.loads(path.read_text()) if path.is_file() else {}
