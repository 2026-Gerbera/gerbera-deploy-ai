"""build_image 입출력(C2). 소스·저장소·스냅샷은 RunContext에서 코드가 채운다(AI가 고르지 않음)."""

from __future__ import annotations

from pydantic import Field

from ddak.core.contracts.base import ContractModel, TierName, ToolInput
from ddak.core.contracts.enums import Source
from ddak.core.contracts.release import ReleaseArtifacts


class BuildImageInput(ToolInput):
    """run_id + tier. tier는 실행기가 build.<tier> step에서 채운다."""

    tier: TierName


class BuildImageOutput(ContractModel):
    """실행기가 release_artifacts로 컨텍스트(images·release_artifacts)를 바꾼다."""

    release_artifacts: ReleaseArtifacts  # 앞선 build step 결과 + 이번 tier
    candidate_sha: str = Field(pattern=r"^[0-9a-f]{40}$")  # 실제로 빌드한 ai-prod 커밋
    build_id: str | None = None  # CodeBuild 빌드 ID(FAKE면 가짜 ID)
    source: Source = Source.LIVE
