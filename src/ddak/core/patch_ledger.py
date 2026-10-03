"""승인 트리의 파일별 패치 재사용과 손실 방지. AI·툴 import 없음."""

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
from ddak.core.patch_patterns import scan_patch_targets
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


def reuse_patches(
    source: Path, previous: Mapping[str, Mapping[str, Any]], runs_root: Path
) -> tuple[bytes | None, list[str]]:
    """환경별 성공 원장. 서로 다른 결과를 요구하면 임의 선택하지 않는다."""
    manifest = file_manifest(source)
    patches: dict[str, bytes] = {}
    changed: set[str] = set()
    for release in previous.values():
        rid = release.get("release_id")
        if not isinstance(rid, str) or Path(rid).name != rid or rid in (".", ".."):
            if release.get("patch_ledger"):
                raise _fail("release_id")
            continue
        for name, entry in release.get("patch_ledger", {}).items():
            if manifest.get(name, {}).get("sha256") != entry["source_sha256"]:
                changed.add(name)
                continue
            digest = entry["patch_sha256"]
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise _fail(name)
            path = runs_root / rid / "patches" / (digest[7:] + ".diff")
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                raise _fail(name)
            data = path.read_bytes()
            if digest_bytes(data) != digest or (name in patches and patches[name] != data):
                raise _fail(name)
            with tempfile.TemporaryDirectory(prefix="ddak-reuse-") as tmp:
                built = Path(tmp) / "source"
                copy_source(source, built)
                apply_diff(built, data)
                if file_manifest(built).get(name, {}).get("sha256") != entry["result_sha256"]:
                    raise _fail(name)
            patches[name] = data
    # 한 환경은 같은 원본, 다른 환경은 변경 원본이면 새 제안 단계에서 합의한다.
    if changed & patches.keys():
        raise _fail(", ".join(sorted(changed & patches.keys())))
    return b"".join(patches[k] for k in sorted(patches)) or None, sorted(changed)


def guard_patch_loss(source: Path, built: Path, previous: Mapping[str, Mapping[str, Any]]) -> None:
    before, after = file_manifest(source), file_manifest(built)
    remaining = {t.file for t in scan_patch_targets(built, ()) if t.severity == "patch"}
    for release in previous.values():
        for name, entry in release.get("patch_ledger", {}).items():
            if name not in after or name in remaining:
                raise _fail(name)
            if before[name]["sha256"] == entry["source_sha256"]:
                if after[name]["sha256"] != entry["result_sha256"]:
                    raise _fail(name)
            else:
                hashes = {
                    digest_bytes(line.strip()) for line in (built / name).read_bytes().splitlines()
                }
                if hashes & set(entry["removed_lines"]):
                    raise _fail(name)
                if after[name]["sha256"] == entry["result_sha256"]:
                    continue  # 의미 증거가 없는 구 원장도 정확한 승인 결과는 허용한다.
                expected = entry.get("required_env_expressions")
                if not isinstance(expected, dict) or not expected:
                    raise _fail(name)
                sites, required = _semantic_sites((built / name).read_bytes())
                for owner, expressions in expected.items():
                    if (
                        not isinstance(owner, str)
                        or not re.fullmatch(r"sha256:[0-9a-f]{64}", owner)
                        or not isinstance(expressions, list)
                        or not expressions
                        or any(
                            not isinstance(value, str)
                            or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
                            for value in expressions
                        )
                        or owner not in required
                        or sites.get(owner) != expressions
                    ):
                        raise _fail(name)


def read_ledger(directory: Path) -> dict[str, Any]:
    path = directory / "patch-ledger.json"
    return json.loads(path.read_text()) if path.is_file() else {}
