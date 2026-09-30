"""한 승인 동작의 대상별 기록. 기록의 인증·저장은 승인 API의 책임이다."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from ddak.core.contracts.base import ContractModel, RunId
from ddak.core.contracts.release import Sha256, SnapshotBinding

NonEmpty = Annotated[str, Field(min_length=1, pattern=r"\S")]


class ApprovalRecord(ContractModel):
    run_id: RunId
    project: NonEmpty
    approval_id: NonEmpty
    kind: Literal["patch", "deploy", "infra", "dockerfile", "foundation"]
    bound_to: Sha256
    approver: NonEmpty
    approved_at: AwareDatetime
    decision: Literal["approved", "denied"]
    snapshot: SnapshotBinding | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.kind == "patch":
            if self.snapshot is None or self.snapshot.patch_sha256 != self.bound_to:
                raise ValueError("패치 승인은 원본·수정본·동일 diff 해시에 묶어야 한다")
        elif self.snapshot is not None:
            raise ValueError("스냅샷 결합은 패치 승인에만 사용한다")
        return self

    def can_reuse_patch(self, project: str, snapshot: SnapshotBinding) -> bool:
        """신뢰할 수 있는 저장소에서 읽은 패치 승인에만 호출한다."""
        return (
            self.kind == "patch"
            and self.decision == "approved"
            and self.project == project
            and self.snapshot == snapshot
        )
