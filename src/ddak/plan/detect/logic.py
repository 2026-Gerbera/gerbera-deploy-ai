"""변경 탐지 순수 로직. Store를 열지 않는다(이전 manifest는 호출자가 넘긴다)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from ddak.core.contracts.base import TierName
from ddak.core.contracts.deploy_config import DeployConfig
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan_facts import Env, FileMeta
from ddak.core.snapshots import digest_json, file_manifest

_MIGRATION_ID = re.compile(r"^(\d+)")


def _in_scope(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        p in (".", "") or path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes
    )


def _scoped(m: Mapping[str, FileMeta], prefixes: tuple[str, ...]) -> dict[str, FileMeta]:
    return {k: v for k, v in m.items() if _in_scope(k, prefixes)}


def resolve_source(source_dir: str, root: Path | None = None) -> Path:
    if Path(source_dir).is_absolute() or ".." in Path(source_dir).parts:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "source_dir 형식이 올바르지 않다")
    base = root if root is not None else Path(os.environ.get("DDAK_SOURCES_DIR") or "var/sources")
    return base / source_dir


def read_manifest(source: Path) -> dict[str, FileMeta]:
    try:
        raw = file_manifest(source)
    except (ValueError, OSError):  # 메시지에 경로를 넣지 않는다
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "소스를 읽을 수 없다") from None
    return {k: FileMeta.model_validate(v) for k, v in raw.items()}


def facts_hash(manifest: Mapping[str, FileMeta]) -> str:
    # executor.service.source_facts 와 같은 식: digest_json(file_manifest(root))
    return digest_json({k: v.model_dump(mode="json") for k, v in manifest.items()})


def parse_config(deploy_config: Mapping[str, object]) -> DeployConfig:
    try:
        return DeployConfig.model_validate(dict(deploy_config))
    except ValidationError:
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED, "deploy_config가 올바르지 않다"
        ) from None


def changed_tiers(
    cfg: DeployConfig,
    current: Mapping[str, FileMeta],
    previous: Mapping[Env, Mapping[str, FileMeta] | None],
) -> dict[Env, dict[TierName, bool]]:
    out: dict[Env, dict[TierName, bool]] = {}
    for env, prev in previous.items():
        out[env] = {
            tier: prev is None or _scoped(current, tc.paths) != _scoped(prev, tc.paths)
            for tier, tc in cfg.tiers.items()
        }
    return out


def changed_paths(
    current: Mapping[str, FileMeta], previous: Mapping[Env, Mapping[str, FileMeta] | None]
) -> tuple[str, ...]:
    prevs = [p for p in previous.values() if p is not None]
    if len(prevs) < len(previous) or not prevs:  # 기록 없는 환경이 있으면 전부 신규
        return tuple(sorted(current))
    paths: set[str] = set()
    for p in prevs:
        paths |= {k for k in current.keys() | p.keys() if current.get(k) != p.get(k)}
    return tuple(sorted(paths))


def new_migrations(
    cfg: DeployConfig,
    current: Mapping[str, FileMeta],
    previous: Mapping[Env, Mapping[str, FileMeta] | None],
) -> tuple[str, ...]:
    ids: set[str] = set()
    for path, mid in _migration_files(cfg, current):
        if any(p is None or path not in p for p in previous.values()):
            ids.add(mid)
    return tuple(sorted(ids))


def modified_migrations(
    cfg: DeployConfig,
    current: Mapping[str, FileMeta],
    previous: Mapping[Env, Mapping[str, FileMeta] | None],
) -> tuple[str, ...]:
    """이미 적용된 경로(이전 manifest에 있음)인데 sha256이 달라진 마이그레이션 id."""
    ids = {
        mid
        for path, mid in _migration_files(cfg, current)
        for p in previous.values()
        if p is not None and path in p and p[path].sha256 != current[path].sha256
    }
    return tuple(sorted(ids))


def _migration_files(cfg: DeployConfig, current: Mapping[str, FileMeta]) -> list[tuple[str, str]]:
    """(경로, id). .sql이 아닌 파일은 무시, 숫자 접두 없는 .sql은 실패(조용히 놓치지 않는다)."""
    if not cfg.migrations_dir:
        return []
    prefix = cfg.migrations_dir.rstrip("/") + "/"
    out: list[tuple[str, str]] = []
    for path in current:
        if not path.startswith(prefix) or not path.endswith(".sql"):
            continue
        name = path[len(prefix) :]
        m = _MIGRATION_ID.match(name)
        if not m:
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, f"마이그레이션 파일명에 숫자 접두가 필요하다: {name}"
            )
        out.append((path, m.group(1)))
    return out
