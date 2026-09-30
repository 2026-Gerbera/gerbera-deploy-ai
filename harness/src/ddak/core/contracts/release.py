"""원본·빌드 스냅샷과 배포 이미지의 결합. 관측이 없으면 아직 미검증이다."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ddak.core.contracts.base import ContractModel, TierName

Sha256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Platform = Literal["linux/amd64", "linux/arm64"]
Target = Literal["local", "cloud"]


class SnapshotBinding(ContractModel):
    source_snapshot_hash: Sha256
    build_snapshot_hash: Sha256
    patch_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_unpatched_snapshot(self) -> Self:
        if self.patch_sha256 is None and self.source_snapshot_hash != self.build_snapshot_hash:
            raise ValueError("패치가 없으면 원본과 빌드 스냅샷이 같아야 한다")
        return self


class ImageArtifact(ContractModel):
    ref: Annotated[str, Field(pattern=r"^[^\s@]+@sha256:[0-9a-f]{64}$")]
    index_digest: Sha256
    platform_digests: dict[Platform, Sha256]

    @model_validator(mode="after")
    def validate_index(self) -> Self:
        if self.ref.rsplit("@", 1)[1] != self.index_digest:
            raise ValueError("이미지 참조는 index digest에 고정해야 한다")
        if set(self.platform_digests) != {"linux/amd64", "linux/arm64"}:
            raise ValueError("amd64와 arm64 플랫폼 digest가 모두 필요하다")
        return self


class ImageObservation(ContractModel):
    platform: Platform
    platform_digest: Sha256


class ReleaseArtifacts(ContractModel):
    snapshot: SnapshotBinding
    images: dict[TierName, ImageArtifact]
    observations: dict[Target, dict[TierName, ImageObservation]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_observations(self) -> Self:
        for tiers in self.observations.values():
            for tier, observation in tiers.items():
                artifact = self.images.get(tier)
                if artifact is None:
                    raise ValueError(f"관측한 tier의 이미지가 없다: {tier}")
                if artifact.platform_digests[observation.platform] != observation.platform_digest:
                    raise ValueError(f"실제 플랫폼 digest가 빌드 결과와 다르다: {tier}")
        return self
