"""receive_deploy_request: 요청 접수. 저장소를 받아 커밋을 고정하고 스냅샷을 만든다."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field

from ddak.core.contracts.base import ContractModel, RunId, ToolInput
from ddak.core.contracts.deploy_request import DeployRequest, RepoUrl
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.release import SnapshotBinding


class ReceiveDeployRequestInput(ToolInput):
    request: DeployRequest


class ReceiveDeployRequestOutput(ContractModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    run_id: RunId
    project: str
    mode: RunMode
    target: Literal["local", "cloud", "both"]
    repo_url: RepoUrl  # 자격증명 없음
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")  # 접수 시점 고정
    source_dir: str  # 작업 루트 기준 상대 경로(run별 checkout)
    snapshot: SnapshotBinding
    file_count: int = Field(ge=0)
    ignored_symlinks: int = Field(ge=0)
    deploy_config: dict[str, Any]  # 검증된 DeployConfig dump
