"""업로드 저장소의 정적 증거와 생성·제거 의도. 소스 값은 결과에 담지 않는다."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

STORAGE_ENV_KEY = "IMG_DIR"
OUTPUT_KEY = "upload_bucket"
STORAGE_SMOKE_GROUP = "storage"
UPLOAD_BUCKET_PATTERN = r"ddak-[a-z][a-z0-9-]{0,36}-uploads-[0-9]{12}"


@dataclass(frozen=True)
class StorageEvidence:
    file: str
    line: int
    kind: Literal["hardcoded_dir", "env_read", "file_write"]


def bucket_name(platform: str, account_id: str) -> str:
    name = f"ddak-{platform}-uploads-{account_id}"
    if not re.fullmatch(UPLOAD_BUCKET_PATTERN, name):
        raise ValueError("업로드 버킷 이름에는 유효한 플랫폼 이름과 12자리 계정이 필요하다")
    return name


def storage_intent(needs: bool, present: bool) -> Literal["create", "remove"] | None:
    if needs == present:
        return None
    return "create" if needs else "remove"


def scan_storage(files: Mapping[str, str]) -> list[StorageEvidence]:
    evidence: set[StorageEvidence] = set()
    for file, text in sorted(files.items()):
        if not file.endswith(".py"):
            continue
        try:
            tree = ast.parse(text, filename=file)
        except (SyntaxError, ValueError):
            continue
        os_names = {"os"}
        env_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                os_names.update(a.asname or a.name for a in node.names if a.name == "os")
            elif isinstance(node, ast.ImportFrom) and node.module == "os":
                env_names.update(a.asname or a.name for a in node.names if a.name == "environ")

        def environ(node: ast.AST, env_names=env_names, os_names=os_names) -> bool:
            return (isinstance(node, ast.Name) and node.id in env_names) or (
                isinstance(node, ast.Attribute)
                and node.attr == "environ"
                and isinstance(node.value, ast.Name)
                and node.value.id in os_names
            )

        bound = {STORAGE_ENV_KEY}
        reads: set[ast.AST] = set()
        assignments = []
        for node in ast.walk(tree):
            key = None
            if isinstance(node, ast.Subscript) and environ(node.value):
                key = node.slice
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and environ(node.func.value)
                and node.args
            ):
                key = node.args[0]
            if (
                isinstance(node, (ast.Call, ast.Subscript))
                and isinstance(key, ast.Constant)
                and key.value == STORAGE_ENV_KEY
            ):
                evidence.add(StorageEvidence(file, node.lineno, "env_read"))
                reads.add(node)
            if isinstance(node, ast.Assign):
                assignments.append((node.targets, node.value))
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                assignments.append(([node.target], node.value))
        for targets, value in assignments:
            if (
                any(isinstance(t, ast.Name) and t.id == STORAGE_ENV_KEY for t in targets)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and value.value
                and "://" not in value.value
            ):
                evidence.add(StorageEvidence(file, value.lineno, "hardcoded_dir"))

        def refers(node: ast.AST, reads=reads, bound=bound) -> bool:
            return any(
                n in reads or (isinstance(n, ast.Name) and n.id in bound) for n in ast.walk(node)
            )

        # Path(IMG_DIR) / name, join과 단순 별칭을 따라간다.
        while True:
            old = set(bound)
            for targets, value in assignments:
                if refers(value):
                    bound.update(t.id for t in targets if isinstance(t, ast.Name))
            if old == bound:
                break
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            write = isinstance(fn, ast.Attribute) and (
                (fn.attr == "save" and any(refers(a) for a in node.args))
                or (fn.attr in {"write", "write_bytes", "write_text"} and refers(fn.value))
            )
            if isinstance(fn, ast.Name) and fn.id == "open" and node.args and refers(node.args[0]):
                mode = (
                    node.args[1]
                    if len(node.args) > 1
                    else next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
                )
                write = (
                    isinstance(mode, ast.Constant)
                    and isinstance(mode.value, str)
                    and any(flag in mode.value for flag in "wax+")
                )
            if write:
                evidence.add(StorageEvidence(file, node.lineno, "file_write"))
    return sorted(evidence, key=lambda e: (e.file, e.line, e.kind))
