"""detect_changed_tiers: 환경별 마지막 성공 배포의 파일 해시와 비교해 변경 tier를 찾는다."""

from __future__ import annotations

from ddak.core.contracts.base import ContractModel, TierName, ToolInput
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.plan_facts import Env, FileMeta
from ddak.core.contracts.release import Sha256, SnapshotBinding


class DetectChangedTiersInput(ToolInput):
    request: DeployRequest
    source_dir: str
    snapshot: SnapshotBinding
    previous: dict[Env, dict[str, FileMeta] | None]  # None = 성공 기록 없음


class DetectChangedTiersOutput(ContractModel):
    changed: dict[Env, dict[TierName, bool]]
    new_migrations: tuple[str, ...]
    modified_migrations: tuple[str, ...] = ()
    changed_paths: tuple[str, ...]
    facts_hash: Sha256
    manifest: dict[str, FileMeta]
