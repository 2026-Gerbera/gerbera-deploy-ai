"""analyze_project: tier·환경 키 분류·Dockerfile 유무 분석(AI는 키 분류만 제안)."""

from __future__ import annotations

from ddak.core.contracts.base import AIUsage, ContractModel, TierName, ToolInput
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.enums import Source
from ddak.core.contracts.plan_facts import Env, EnvKey, PatchTarget


class AnalyzeProjectInput(ToolInput):
    request: DeployRequest
    source_dir: str
    changed: dict[Env, dict[TierName, bool]]
    new_migrations: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()


class AnalyzeProjectOutput(ContractModel):
    tiers: tuple[TierName, ...]
    env_keys: tuple[EnvKey, ...]
    has_dockerfile: dict[TierName, bool]
    infra_inputs_changed: bool
    patch_targets: tuple[PatchTarget, ...] = ()
    smoke_groups: tuple[str, ...] = ()
    source: Source | None = None
    ai_usage: AIUsage | None = None
