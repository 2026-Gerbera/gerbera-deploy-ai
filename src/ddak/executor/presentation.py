"""기존 실행 기록의 표시용 투영. 값과 코드 원문은 반환하지 않는다."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import re
import textwrap
import tokenize
from pathlib import Path

from ddak.core.runlog import run_dir
from ddak.core.runtime_values import derived_public, generated_secret


def masked_code(code: str) -> str:
    """리터럴·주석은 숨기고 환경변수 조회의 키 이름만 남긴다."""
    code = textwrap.dedent(code)
    allowed = set()
    try:
        for node in ast.walk(ast.parse(code)):
            key = None
            if isinstance(node, ast.Subscript) and ast.unparse(node.value) in {
                "os.environ",
                "environ",
            }:
                key = node.slice
            if isinstance(node, ast.Call) and ast.unparse(node.func) in {
                "os.getenv",
                "getenv",
                "os.environ.get",
                "environ.get",
            }:
                key = node.args[0] if node.args else None
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key.value)
            ):
                allowed.add((key.lineno, key.col_offset))
        starts = [0]
        for line in code.splitlines(keepends=True):
            starts.append(starts[-1] + len(line))
        spans = []
        for item in tokenize.generate_tokens(io.StringIO(code).readline):
            value = item.string
            if (
                item.type
                in (tokenize.STRING, tokenize.NUMBER, getattr(tokenize, "FSTRING_MIDDLE", -1))
                and item.start not in allowed
            ):
                value = '"[가림]"'
            elif item.type == tokenize.COMMENT:
                value = "# [가림]"
            if value != item.string:
                spans.append(
                    (
                        starts[item.start[0] - 1] + item.start[1],
                        starts[item.end[0] - 1] + item.end[1],
                        value,
                    )
                )
        for start, end, value in reversed(spans):
            code = code[:start] + value + code[end:]
        return code
    except (SyntaxError, ValueError, tokenize.TokenError, IndentationError):
        return "[구문 일부만 기록되어 코드 내용을 가렸습니다]"


def patch_preview(patch: str | None) -> list[dict]:
    files = []
    current = None
    for line in (patch or "").splitlines():
        if line.startswith("--- a/"):
            current = {"file": line[6:], "before": [], "after": [], "line": 1}
            files.append(current)
        elif current is not None:
            if line.startswith("@@"):
                match = re.match(r"@@ -(\d+)", line)
                current["line"] = int(match[1]) if match else 1
            elif not line.startswith(("+++ ", "\\")) and line:
                if line[0] in " -":
                    current["before"].append(line[1:])
                if line[0] in " +":
                    current["after"].append(line[1:])
    return [
        {
            **f,
            "before": masked_code("\n".join(f["before"])),
            "after": masked_code("\n".join(f["after"])),
        }
        for f in files
    ]


def display_data(store, root: Path, run_id: str) -> dict:
    prepared = store.prepared(run_id) or {}
    context = prepared.get("context", {})
    directory = run_dir(root / "runs", run_id)
    facts = {}
    with contextlib.suppress(OSError, ValueError):
        facts = json.loads((directory / "facts.json").read_text())
    plan = prepared.get("plan", {})
    previous = context.get("previous_release", {})
    with contextlib.suppress(OSError, ValueError):
        recorded = json.loads((directory / "context.json").read_text())
        previous = recorded.get("previous_release", previous)
    if store.run(run_id)["status"] in {"AWAITING_APPROVAL", "APPROVED"}:
        previous = {
            env: row.get("current") or {}
            for env, row in store.environments(store.run(run_id)["project"]).items()
        }
    current = prepared.get("source_files", {})
    tiers = context.get("deploy_config", {}).get("tiers", {})
    changes = []
    for env in ("local", "cloud"):
        if not plan.get("deploy", {}).get(env, {}).get("steps"):
            continue
        old = previous.get(env, {}).get("source_files", {})
        for name in sorted(current.keys() | old.keys()):
            if current.get(name) == old.get(name):
                continue
            owners = [
                tier.upper()
                for tier, cfg in tiers.items()
                if any(
                    p == "." or name == p or name.startswith(p.rstrip("/") + "/")
                    for p in cfg.get("paths", ["."])
                )
            ]
            changes.append(
                {
                    "file": name,
                    "tier": " · ".join(owners) or "공통",
                    "target": env,
                    "change": "삭제"
                    if name not in current
                    else "추가"
                    if name not in old
                    else "수정",
                }
            )
    keys = [k["name"] for k in facts.get("env_keys", []) if isinstance(k, dict) and "name" in k]
    meta = json.loads(prepared.get("patch_meta_json", "null")) or {}
    keys = sorted(set(keys + meta.get("new_env_keys", []) + context.get("required_env_keys", [])))
    platforms = context.get("platform", {})
    mappings = []
    for key in keys:
        row = {"key": key, "local": "출처 미확인", "cloud": "출처 미확인"}
        local = platforms.get("onprem", {})
        public_keys = set(local.get("tiers", {}).get("was", {}).get("public_env", {})) | set(
            derived_public(local, keys)
        )
        if key in public_keys:
            row["local"] = "인벤토리"
        elif generated_secret(key):
            row["local"] = "배포 시 자동 생성·보관"
        elif local.get("tiers", {}).get("was", {}).get("env_file"):
            row["local"] = "관리 페이지 · 저장 여부는 연결 검사에서 확인"
        cloud = platforms.get("cloud", {})
        if (
            key in cloud.get("env", {})
            or key in cloud.get("env_plain", {})
            or key in cloud.get("env_secret", {})
        ):
            row["cloud"] = "Terraform 출력"
        elif key in context.get("required_env_keys", []):
            row["cloud"] = "관리 페이지 · 저장 여부는 연결 검사에서 확인"
        mappings.append(row)
    return {
        "files": changes,
        "patches": patch_preview(prepared.get("patch")),
        "findings": [
            {k: f.get(k) for k in ("file", "line", "pattern_id", "key")}
            for f in facts.get("patch_targets", [])
        ],
        "mappings": mappings,
        "previous": {
            env: {"source_sha": entry.get("source_sha"), "images": entry.get("images", {})}
            for env, entry in previous.items()
        },
        "plan": plan,
    }
