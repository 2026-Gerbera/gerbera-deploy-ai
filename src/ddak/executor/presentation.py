"""기존 실행 기록의 표시용 투영. 값과 코드 원문은 반환하지 않는다."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from ddak.core.code_mask import code_changes
from ddak.core.code_mask import masked_code as masked_code
from ddak.core.runlog import run_dir
from ddak.core.runtime_values import derived_public, generated_secret


def patch_preview(patch: str | None, source: Path | None = None) -> list[dict]:
    return code_changes(patch, source)


def display_data(store, root: Path, run_id: str) -> dict:
    prepared = store.prepared(run_id) or {}
    context = prepared.get("context", {})
    directory = run_dir(root / "runs", run_id)
    facts = {}
    with contextlib.suppress(OSError, ValueError):
        facts = json.loads((directory / "facts.json").read_text())
    original_analysis = False
    parent = (store.patch_review(run_id) or {}).get("parent")
    if not facts.get("patch_targets") and parent:
        with contextlib.suppress(OSError, ValueError):
            inherited = json.loads((run_dir(root / "runs", parent) / "facts.json").read_text())
            facts["patch_targets"] = inherited.get("patch_targets", [])
            original_analysis = True
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
        "patches": patch_preview(prepared.get("patch"), root / "sources" / run_id),
        "findings": [
            {k: f.get(k) for k in ("file", "line", "pattern_id", "key", "severity", "is_new")}
            for f in facts.get("patch_targets", [])
        ],
        "mappings": mappings,
        "original_analysis": original_analysis,
        "previous": {
            env: {
                "source_sha": entry.get("source_sha"),
                "images": entry.get("images", {}),
                **{key: entry[key] for key in ("ref", "version") if entry.get(key)},
            }
            for env, entry in previous.items()
        },
        "plan": plan,
    }
