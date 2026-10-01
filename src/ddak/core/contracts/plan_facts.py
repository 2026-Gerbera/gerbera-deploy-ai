"""코드가 만든 사실(Facts). planner·validate 입력이며 Plan.facts_hash의 근거다."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ddak.core.contracts.base import ContractModel, TierName
from ddak.core.contracts.enums import By, RunMode
from ddak.core.contracts.release import Sha256

__all__ = ["Env", "EnvKey", "Facts", "FileMeta"]

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
    by: By = By.RULE
    reason: str | None = Field(default=None, max_length=200)


class Facts(ContractModel):
    project: str
    mode: RunMode
    target: Literal["local", "cloud", "both"]
    tiers: tuple[TierName, ...]
    changed: dict[Env, dict[TierName, bool]]  # 환경별 tier 변경 여부(성공 기록 없으면 True)
    new_migrations: tuple[str, ...] = ()  # 예: "0002"
    env_keys: tuple[EnvKey, ...] = ()
    db_initialized: dict[Env, bool] = Field(default_factory=dict)
    has_dockerfile: dict[TierName, bool] = Field(default_factory=dict)
    infra_inputs_changed: bool = False  # 새 secret 키가 있으면 True(분석이 계산)
    code_patch: bool = False
    source_snapshot_hash: Sha256
    facts_hash: Sha256  # detect가 만든 값(= executor.service.source_facts와 같은 식)
