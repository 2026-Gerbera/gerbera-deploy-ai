"""승인 기록의 결합과 패치 승인 재사용. UI·저장소 구현은 요구하지 않는다."""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from pydantic import ValidationError

from tests.support import REPO_ROOT

SOURCE = "sha256:" + "a" * 64
BUILD = "sha256:" + "b" * 64
PATCH = "sha256:" + "c" * 64
OTHER = "sha256:" + "d" * 64
KINDS = ("deploy", "patch", "infra", "dockerfile", "foundation")


def _model(module: str, name: str) -> Any:
    path = REPO_ROOT / "src/ddak/core/contracts" / f"{module}.py"
    assert path.is_file(), f"{module}.py 계약이 아직 구현되지 않았다"
    model = getattr(importlib.import_module(f"ddak.core.contracts.{module}"), name, None)
    assert isinstance(model, type), f"계약 {name}이 아직 구현되지 않았다"
    return model


def _snapshot(**changes: Any) -> Any:
    return _model("release", "SnapshotBinding")(
        **{
            "source_snapshot_hash": SOURCE,
            "build_snapshot_hash": BUILD,
            "patch_sha256": PATCH,
            **changes,
        }
    )


def _record(**changes: Any) -> Any:
    payload = {
        "run_id": "run-1",
        "project": "sample-app",
        "approval_id": "approval-1",
        "kind": "patch",
        "bound_to": PATCH,
        "approver": "operator",
        "approved_at": "2026-09-30T08:00:00Z",
        "decision": "approved",
        "snapshot": _snapshot(),
    }
    return _model("approval", "ApprovalRecord")(**{**payload, **changes})


def test_one_approval_id_can_have_separate_records_for_every_kind() -> None:
    records = [
        _record(kind=kind, snapshot=_snapshot() if kind == "patch" else None) for kind in KINDS
    ]
    assert {record.approval_id for record in records} == {"approval-1"}
    assert {record.kind for record in records} == set(KINDS)
    for record in records:
        data = record.model_dump(mode="json")
        assert data["run_id"] == "run-1"
        assert data["project"] == "sample-app"
        assert data["bound_to"] == PATCH
        assert data["approver"] == "operator"
        assert data["approved_at"]
        assert data["decision"] == "approved"


def test_patch_requires_matching_snapshot_and_other_kinds_forbid_snapshot() -> None:
    # 모델 부재는 아래 raises가 삼키지 않는 assertion으로 먼저 드러난다.
    _record()
    for snapshot in (
        None,
        _snapshot(patch_sha256=OTHER),
        _snapshot(build_snapshot_hash=SOURCE, patch_sha256=None),
    ):
        with pytest.raises(ValidationError):
            _record(snapshot=snapshot)
    for kind in ("deploy", "infra", "dockerfile", "foundation"):
        with pytest.raises(ValidationError):
            _record(kind=kind)


def test_approval_rejects_unknown_kind_decision_and_malformed_binding() -> None:
    _record()
    for changes in (
        {"kind": "other"},
        {"decision": "pending"},
        {"kind": "deploy", "snapshot": None, "bound_to": "sha256:" + "A" * 64},
        {"kind": "deploy", "snapshot": None, "bound_to": "sha256:short"},
    ):
        with pytest.raises(ValidationError):
            _record(**changes)


def test_patch_reuse_requires_same_project_and_all_three_hashes_but_not_run() -> None:
    record = _record()
    reuse = getattr(record, "can_reuse_patch", None)
    assert callable(reuse), "ApprovalRecord.can_reuse_patch가 아직 구현되지 않았다"
    assert reuse("sample-app", _snapshot()) is True
    assert _record(run_id="run-2").can_reuse_patch("sample-app", _snapshot()) is True
    assert reuse("other-project", _snapshot()) is False
    for field in ("source_snapshot_hash", "build_snapshot_hash", "patch_sha256"):
        assert reuse("sample-app", _snapshot(**{field: OTHER})) is False
    assert reuse("sample-app", _snapshot(build_snapshot_hash=SOURCE, patch_sha256=None)) is False


def test_denied_patch_and_nonpatch_approvals_cannot_be_reused_as_patch() -> None:
    record = _record(decision="denied")
    reuse = getattr(record, "can_reuse_patch", None)
    assert callable(reuse), "ApprovalRecord.can_reuse_patch가 아직 구현되지 않았다"
    assert reuse("sample-app", _snapshot()) is False
    for kind in ("deploy", "infra", "dockerfile", "foundation"):
        assert _record(kind=kind, snapshot=None).can_reuse_patch("sample-app", _snapshot()) is False
