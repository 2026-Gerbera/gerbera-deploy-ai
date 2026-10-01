"""receive_deploy_request 로직: 검증 -> fetch -> 링크 제거 -> 소스 검사 -> deploy.yaml -> 해시."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_config import DeployConfig
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.release import SnapshotBinding
from ddak.core.contracts.tools.receive_deploy_request import (
    ReceiveDeployRequestInput,
    ReceiveDeployRequestOutput,
)
from ddak.core.logging import get_logger
from ddak.core.snapshots import digest_json, file_manifest
from ddak.plan.intake.fetch import Checkout, check_ref, check_url, fetch_repo, fetched_at
from ddak.plan.intake.policy import FetchPolicy

_log = get_logger("plan")
_LFS = b"version https://git-lfs"
_MAX_YAML = 256 * 1024
Fetcher = Callable[..., Checkout]


def _remove_symlinks(root: Path) -> list[str]:
    """F10: 링크는 따라가지 않고 run 사본에서 삭제한다. 삭제한 상대 경로를 돌려준다."""
    removed: list[str] = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in [*dirs, *names]:
            path = Path(directory) / name
            if path.is_symlink():
                removed.append(path.relative_to(root).as_posix())
                path.unlink()
    return sorted(removed)


def _inspect(root: Path, policy: FetchPolicy) -> None:
    """F8(크기·파일 수)·F9(서브모듈·LFS)."""
    total = count = 0
    for directory, _dirs, names in os.walk(root, followlinks=False):
        for name in names:
            path = Path(directory) / name
            size = path.stat().st_size
            total += size
            count += 1
            if total > policy.max_bytes or count > policy.max_files:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "저장소가 너무 크다(용량 또는 파일 수 한도 초과)"
                )
            if name == ".gitmodules":
                raise DdakToolError(ErrorCode.CONFIG_INVALID, "git 서브모듈은 지원하지 않는다")
            if size < 1024 and path.read_bytes().startswith(_LFS):
                raise DdakToolError(ErrorCode.CONFIG_INVALID, "Git LFS 파일은 지원하지 않는다")


def _hint(rel: str, ignored: list[str]) -> str:
    hit = any(rel == i or rel.startswith(i + "/") for i in ignored)
    return " (심볼릭 링크라 제외됐다)" if hit else ""


def _load_config(root: Path, ignored: list[str]) -> DeployConfig:
    """F11: 저장소 루트의 deploy.yaml만 본다."""
    path = root / "deploy.yaml"
    if not path.is_file():
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "저장소 루트에 deploy.yaml이 필요하다(예시: fixtures/deploy.example.yaml)"
            + _hint("deploy.yaml", ignored),
        )
    if path.stat().st_size > _MAX_YAML:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "deploy.yaml이 너무 크다")
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "deploy.yaml을 해석할 수 없다") from None
    if not isinstance(data, dict):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "deploy.yaml 최상위는 매핑이어야 한다")
    try:
        return DeployConfig.model_validate(data)
    except ValidationError as e:  # 필드 위치만 보여준다(값은 숨김)
        where = ", ".join(".".join(str(p) for p in err["loc"]) or "(root)" for err in e.errors())
        raise DdakToolError(ErrorCode.CONFIG_INVALID, f"deploy.yaml 필드 오류: {where}") from None


def _check_paths(root: Path, cfg: DeployConfig, ignored: list[str]) -> None:
    """F12: 선언한 경로가 checkout에 실제로 있어야 한다(dockerfile=None은 정상)."""
    real = root.resolve()
    wanted: list[tuple[str, str]] = []
    for tier, t in cfg.tiers.items():
        wanted += [(f"tier {tier} paths", p) for p in t.paths]
        if t.dockerfile:
            wanted.append((f"tier {tier} dockerfile", t.dockerfile))
    if cfg.migrations_dir:
        wanted.append(("migrations_dir", cfg.migrations_dir))
    if cfg.env_example:
        wanted.append(("env_example", cfg.env_example))
    for label, rel in wanted:
        target = (root / rel).resolve()
        if not target.is_relative_to(real) or not target.exists():
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID,
                f"{label}: '{rel}'이(가) 저장소에 없다" + _hint(rel.rstrip("/"), ignored),
            )


def receive(
    inp: ReceiveDeployRequestInput,
    ctx: RunContext,
    *,
    policy: FetchPolicy | None = None,
    fetcher: Fetcher = fetch_repo,
) -> ReceiveDeployRequestOutput:
    req = inp.request
    policy = policy or FetchPolicy.from_env()
    url = check_url(req.repo_url, policy)  # F1
    ref = check_ref(req.ref)  # F2
    checkout = fetcher(url, ref, run_id=inp.run_id, project=req.project, policy=policy)
    root = checkout.path
    try:
        ignored = _remove_symlinks(root)
        if ignored:
            _log.warning("심볼릭 링크를 무시했다", count=len(ignored), paths=ignored[:5])
        _inspect(root, policy)
        cfg = _load_config(root, ignored)
        _check_paths(root, cfg, ignored)
        try:
            manifest = file_manifest(root)
        except ValueError:
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "소스에 일반 파일이 아닌 항목이 있다"
            ) from None
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)  # 접수 실패: run 사본 삭제(캐시는 유지)
        raise
    digest = digest_json(manifest)
    return ReceiveDeployRequestOutput(
        run_id=inp.run_id,
        project=req.project,
        mode=req.mode,
        target=req.target,
        repo_url=url,
        commit=checkout.commit,
        source_dir=root.relative_to(policy.root).as_posix(),
        snapshot=SnapshotBinding(source_snapshot_hash=digest, build_snapshot_hash=digest),
        file_count=len(manifest),
        ignored_symlinks=len(ignored),
        deploy_config=cfg.model_dump(mode="json"),
    )


def cleanup_stale_sources(
    root: Path,
    max_age_s: float = 3600,
    *,
    keep: Collection[str] = (),
    now: float | None = None,
) -> list[str]:
    """TTL 지난 캐시·run 사본·남은 .partial을 지운다. keep(진행 중 run_id)의 사본은 보존."""
    now = time.time() if now is None else now
    removed: list[str] = []
    candidates = [(p, False) for p in sorted(root.glob("cache/*/*/*")) if p.is_dir()]
    candidates += [(p, True) for p in sorted(root.glob("runs/*/*")) if p.is_dir()]
    for path, is_run in candidates:
        if is_run and path.name in keep:
            continue
        at = None if is_run else fetched_at(path)
        if at is None:
            at = path.stat().st_mtime
        if now - at >= max_age_s:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path.relative_to(root).as_posix())
    return removed
