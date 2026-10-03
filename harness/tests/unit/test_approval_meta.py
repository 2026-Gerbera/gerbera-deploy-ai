"""WP10: 표시 메타는 승인을 대체하지 않고, 인프라 요약은 승인 해시에 묶인다."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.executor.approval_meta import encode_meta
from ddak.executor.engine import RunStatus
from tests.unit.test_deployment_service import PATCH, patch_metadata, plan
from tests.unit.test_deployment_service import rig as rig

HASH = "sha256:" + "a" * 64
pytestmark = pytest.mark.anyio


def summary() -> dict[str, Any]:
    return {
        "layer": "app",
        "plan_sha256": HASH,
        "exit_code": 2,
        "headline": "+ 시크릿 1, IAM 정책 1 변경, 삭제 0",
        "counts": {"create": 1, "update": 1, "delete": 0, "replace": 0},
        "destructive": [],
        "iam_diff": [
            {
                "address": "aws_iam_role_policy.exec_secrets",
                "action": "update",
                "role_path": "/ddak/app/",
                "boundary_attached": True,
                "trust_changed": False,
                "added": [
                    {
                        "actions": ["secretsmanager:GetSecretValue"],
                        "resources": [
                            "arn:aws:secretsmanager:ap-northeast-2:***:secret:ddak/app/key-***"
                        ],
                    }
                ],
            }
        ],
        "access_analyzer": {"errors": 0, "security_warnings": 0},
        "checkov": {"passed": True, "failed": []},
        "sensitive_masked": True,
    }


def prepare(rig: Any, **kwargs: Any) -> str:
    service, source, _ = rig
    p = plan(patch="patch_meta" in kwargs)
    return service.prepare(
        p,
        RunContext(p.run_id, project=p.project, toggles=p.toggles),
        source,
        patch=PATCH if p.toggles["code_patch"] else None,
        **kwargs,
    )


async def test_default_fields_and_old_execution_still_work(rig: Any) -> None:
    service, _, _ = rig
    rid = prepare(rig)
    view = service.approval_view(rid)
    assert set(view) >= {
        "run_id",
        "project",
        "subjects",
        "snapshot",
        "patch",
        "plan",
        "patch_meta",
        "infra_summary",
    }
    assert view["patch_meta"] is None and view["infra_summary"] is None
    service.approve(rid, approver="operator")
    service.start(rid)
    assert (await service.wait(rid)).status is RunStatus.SUCCEEDED


async def test_metadata_roundtrip_one_approval_and_input_isolation(rig: Any) -> None:
    service, _, _ = rig
    infra = summary()
    meta = patch_metadata(reason="개발 URL을 환경 설정에서 읽음", reuse=True, source="cache")
    expected = copy.deepcopy({"patch_meta": meta, "infra_summary": infra})
    rid = prepare(rig, subjects={"infra": HASH}, infra_summary=infra, patch_meta=meta)
    infra["counts"]["delete"] = 10
    meta["reason"] = "changed"
    view = service.approval_view(rid)
    assert {k: view[k] for k in expected} == expected
    view["infra_summary"]["counts"]["delete"] = 99
    path = service.root / "runs" / rid / "approval-meta.json"
    assert json.loads(path.read_text()) == expected
    # reuse=True는 표시만 한다. 새 run의 배포/패치/인프라 승인은 여전히 필요하다.
    with pytest.raises(DdakToolError, match="APPROVAL_REQUIRED"):
        service.start(rid)
    records = service.approve(rid, approver="operator")
    assert {r.kind for r in records} == {"deploy", "patch", "infra"}
    assert len({r.approval_id for r in records}) == 1
    assert next(r for r in records if r.kind == "infra").bound_to == HASH
    service.start(rid)
    assert (await service.wait(rid)).status is RunStatus.SUCCEEDED


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_wrong_summary_prevents_all_approvals(rig: Any, missing: bool) -> None:
    service, _, calls = rig
    infra = None if missing else {**summary(), "plan_sha256": "sha256:" + "b" * 64}
    rid = prepare(rig, subjects={"infra": HASH}, infra_summary=infra)
    with pytest.raises(DdakToolError, match="APPROVAL_REQUIRED"):
        service.approve(rid, approver="operator")
    assert service.store.approvals(rid) == [] and calls.contexts == []
    # 부족한 자료라도 사용자는 거절할 수 있다.
    assert all(r.decision == "denied" for r in service.approve(rid, approver="op", approved=False))


@pytest.mark.parametrize("tamper_after_approval", [False, True])
def test_saved_metadata_tampering_blocks_execution(rig: Any, tamper_after_approval: bool) -> None:
    service, _, calls = rig
    rid = prepare(rig, subjects={"infra": HASH}, infra_summary=summary())
    if tamper_after_approval:
        service.approve(rid, approver="operator")
    (service.root / "runs" / rid / "approval-meta.json").write_text("{}")
    with pytest.raises(DdakToolError, match="APPROVAL_REQUIRED"):
        if tamper_after_approval:
            service.start(rid)
        else:
            service.approve(rid, approver="operator")
    assert calls.contexts == []


@pytest.mark.parametrize(
    "change",
    [
        {"headline": "한" * 3000},
        {"counts": {"create": -1, "update": 0, "delete": 0, "replace": 0}},
        {"headline": "password=" + "do-not-log-this-value"},
        {"iam_diff": [{"credential": "do-not-log-this-value"}]},
        {"sensitive_masked": False},
        {"sensitive_masked": 1},
        {"exit_code": False},
        {"exit_code": 2.0},
        {"counts": {"create": float("nan"), "update": 0, "delete": 0, "replace": 0}},
        {"extra": "unsupported"},
    ],
)
def test_invalid_metadata_rejected_before_persistence(rig: Any, change: dict) -> None:
    service, _, _ = rig
    with pytest.raises(DdakToolError) as exc:
        prepare(rig, subjects={"infra": HASH}, infra_summary={**summary(), **change})
    assert exc.value.code is ErrorCode.CONFIG_INVALID
    assert "do-not-log-this-value" not in str(exc.value)
    directory = service.root / "runs" / "run-2"
    assert {p.name for p in directory.iterdir()} == {"events.jsonl"}
    assert service.store.prepared("run-2") is None
    event = service.events("run-2")[-1]
    assert event["type"] == "stage.finished" and event["status"] == "failed"
    assert "do-not-log-this-value" not in (directory / "events.jsonl").read_text()


@pytest.mark.parametrize(
    "meta",
    [
        {"reason": "x", "reuse": "true", "source": "cache"},
        {"reason": "x", "reuse": False, "source": "unknown"},
        {"reason": "x" * 201, "reuse": False, "source": "live"},
    ],
)
def test_patch_metadata_shape(rig: Any, meta: dict) -> None:
    with pytest.raises(DdakToolError, match="CONFIG_INVALID"):
        prepare(rig, patch_meta=patch_metadata(**meta))


def test_summary_requires_corresponding_subject(rig: Any) -> None:
    with pytest.raises(DdakToolError, match="CONFIG_INVALID"):
        prepare(rig, infra_summary=summary())


@pytest.mark.parametrize("size", [8192, 8193])
def test_exact_utf8_byte_limit(size: int) -> None:
    value = summary()
    value["headline"] = "한"
    base_size = len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
    value["headline"] += "x" * (size - base_size)
    if size == 8192:
        assert len(encode_meta(value, infra=True).encode()) == size
    else:
        with pytest.raises(DdakToolError, match="CONFIG_INVALID"):
            encode_meta(value, infra=True)


@pytest.mark.parametrize("character", ["+", "=", "@"])
def test_masked_secret_arn_resource_characters(character: str) -> None:
    value = summary()
    value["iam_diff"][0]["added"][0]["resources"] = [
        "arn:aws:secretsmanager:ap-northeast-2:***:secret:ddak/app/test" + character + "name"
    ]
    assert json.loads(encode_meta(value, infra=True)) == value


def test_unmasked_account_arn_is_rejected() -> None:
    value = summary()
    value["iam_diff"][0]["added"][0]["resources"] = [
        "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:ddak/app/key"
    ]
    with pytest.raises(DdakToolError, match="CONFIG_INVALID"):
        encode_meta(value, infra=True)


@pytest.mark.parametrize("field", ["reuse", "sensitive_masked"])
def test_saved_boolean_changed_to_number_is_not_equal(rig: Any, field: str) -> None:
    service, _, calls = rig
    rid = prepare(
        rig,
        subjects={"infra": HASH},
        infra_summary=summary(),
        patch_meta=patch_metadata(reason="환경 설정 교체", reuse=False, source="live"),
    )
    service.approve(rid, approver="operator")
    path = service.root / "runs" / rid / "approval-meta.json"
    saved = json.loads(path.read_text())
    section = "patch_meta" if field == "reuse" else "infra_summary"
    saved[section][field] = int(saved[section][field])
    path.write_text(json.dumps(saved))
    with pytest.raises(DdakToolError, match="APPROVAL_REQUIRED"):
        service.start(rid)
    assert calls.contexts == []
