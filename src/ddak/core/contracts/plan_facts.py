"""코드가 만든 사실(Facts). planner·validate 입력이며 Plan.facts_hash의 근거다."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ddak.core.contracts.base import ContractModel, TierName
from ddak.core.contracts.enums import By, RunMode, Source
from ddak.core.contracts.release import Sha256

__all__ = ["Env", "EnvKey", "Facts", "FileMeta", "PatchTarget"]

Env = Literal["local", "cloud"]


class FileMeta(ContractModel):
    """core/store.release_view 의 파일 항목과 같은 모양."""

    sha256: Sha256
    executable: bool


class EnvKey(ContractModel):
    name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    kind: Literal["plain", "secret"]
    tier: TierName | None = None
    is_new: bool = True
    required: bool = True
    by: By = By.RULE
    reason: str | None = Field(default=None, max_length=200)
    source: Source | None = None
    provider: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=200)


class PatchTarget(ContractModel):
    """정적 탐지 위치와 키 이름만 전달한다. 원문·값은 계약에 포함하지 않는다."""

    file: str
    line: int = Field(ge=1)
    pattern_id: str
    is_new: bool
    severity: Literal["patch", "warning"]
    key: str | None = None


class Facts(ContractModel):
    project: str
    mode: RunMode
    target: Literal["local", "cloud", "both"]
    tiers: tuple[TierName, ...]
    changed: dict[Env, dict[TierName, bool]]  # 환경별 tier 변경 여부(성공 기록 없으면 True)
    new_migrations: tuple[str, ...] = ()  # 예: "0002"
    modified_migrations: tuple[str, ...] = ()  # 이미 적용된 id인데 파일 내용이 바뀐 것
    smoke_groups: tuple[str, ...] = ()  # 배포 소스에서 발견한 추가 스모크 그룹
    env_keys: tuple[EnvKey, ...] = ()
    patch_targets: tuple[PatchTarget, ...] = ()
    db_initialized: dict[Env, bool] = Field(default_factory=dict)
    has_dockerfile: dict[TierName, bool] = Field(default_factory=dict)
    infra_inputs_changed: bool = False  # 새 secret 키가 있으면 True(분석이 계산)
    code_patch: bool = False
    source_snapshot_hash: Sha256
    facts_hash: Sha256  # detect가 만든 값(= executor.service.source_facts와 같은 식)
