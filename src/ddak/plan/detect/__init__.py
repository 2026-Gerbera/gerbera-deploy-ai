"""plan/detect: 변경 탐지·스냅샷 해시. 담당 정준우(O1, O2 승계). AI를 import하지 않는다.

공개: detect_changed_tiers(툴 본체), facts_reader(DeploymentService.prepare 용).
"""

from __future__ import annotations

from pathlib import Path

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.detect_changed_tiers import (
    DetectChangedTiersInput,
    DetectChangedTiersOutput,
)
from ddak.core.snapshots import digest_json, file_manifest
from ddak.plan.detect import logic

__all__ = ["detect_changed_tiers", "facts_reader"]


def facts_reader(source: Path) -> str:
    """source_facts 와 같은 식(소스 manifest 해시)."""
    return digest_json(file_manifest(source))


def detect_changed_tiers(
    inp: DetectChangedTiersInput, ctx: RunContext, *, root: Path | None = None
) -> DetectChangedTiersOutput:
    cfg = logic.parse_config(ctx.deploy_config)
    manifest = logic.read_manifest(logic.resolve_source(inp.source_dir, root))
    if digest_json({k: v.model_dump(mode="json") for k, v in manifest.items()}) != (
        inp.snapshot.source_snapshot_hash
    ):
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "접수 이후 소스가 변경됐다")
    envs = ("local", "cloud") if inp.request.target == "both" else (inp.request.target,)
    previous = {env: inp.previous.get(env) for env in envs}
    return DetectChangedTiersOutput(
        changed=logic.changed_tiers(cfg, manifest, previous),
        new_migrations=logic.new_migrations(cfg, manifest, previous),
        modified_migrations=logic.modified_migrations(cfg, manifest, previous),
        changed_paths=logic.changed_paths(manifest, previous),
        facts_hash=logic.facts_hash(manifest),
        manifest=manifest,
    )
