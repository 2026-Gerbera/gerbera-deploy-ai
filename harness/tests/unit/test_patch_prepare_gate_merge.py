"""source=fixture: 패치 검사와 바이트 해시 결합은 설정·토글과 무관한 승인 전 조건이다."""

from pathlib import Path
from typing import Any

import pytest

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.patch_config import PatchConfigOutput, PatchMeta
from ddak.executor.engine import RunStatus
from tests.unit import test_deployment_service as support

pytestmark = pytest.mark.anyio
rig = support.rig
STATES = [
    pytest.param(True, id="on"),
    pytest.param(False, id="off"),
    pytest.param(None, id="absent"),
]


@pytest.fixture
async def patched_release(rig):
    service, source, calls = rig
    run_id = support.prepare(service, source, patch=True)
    service.approve(run_id, approver="fixture")
    service.start(run_id)
    assert (await service.wait(run_id)).status is RunStatus.SUCCEEDED
    assert all(
        row["current"]["patch_ledger"]["app.py"]
        for row in service.store.environments("demo").values()
    )
    calls.contexts.clear()
    return rig


def request(service, setting, toggle):
    if setting is not None:
        service.save_project_settings(
            "demo", {"code_patch": setting}, updated_by="fixture", expected_version=0
        )
    toggles = {} if toggle is None else {"code_patch": toggle}
    plan = support.plan("run-patch-gate").model_copy(update={"toggles": toggles})
    context = RunContext(plan.run_id, project=plan.project, toggles=toggles)
    return plan, context


def assert_no_prepared_patch(rig, run_id, previous):
    service, source, calls = rig
    assert service.store.prepared(run_id) is None
    assert service.store.approvals(run_id) == []
    assert service.store.environments("demo") == previous
    assert calls.contexts == []
    assert (source / "app.py").read_bytes() == b"VERSION = 1\n"
    directory = service.root / "runs" / run_id
    assert {path.name for path in directory.iterdir()} == {"events.jsonl"}
    event = service.events(run_id)[-1]
    assert event["type"] == "stage.finished" and event["status"] == "failed"


@pytest.mark.parametrize("setting", STATES)
@pytest.mark.parametrize("toggle", STATES)
@pytest.mark.parametrize(
    "invalid",
    [
        "meta_missing",
        "passed_missing",
        "passed_false",
        "passed_truthy_one",
        "sha_missing",
        "sha_mismatch",
    ],
)
async def test_unverified_patch_rejected_before_approval_for_every_setting_and_toggle(
    patched_release, setting, toggle, invalid
):
    service, source, _ = patched_release
    plan, context = request(service, setting, toggle)
    previous = service.store.environments("demo")
    meta: dict[str, Any] | None = support.patch_metadata(reuse=True, source="cache")
    if invalid == "meta_missing":
        meta = None
    elif invalid == "passed_missing":
        meta.pop("passed")
    elif invalid == "passed_false":
        meta["passed"] = False
    elif invalid == "passed_truthy_one":
        meta["passed"] = 1
    elif invalid == "sha_missing":
        meta.pop("patch_sha256")
    else:
        meta["patch_sha256"] = "sha256:" + "0" * 64

    with pytest.raises(DdakToolError, match="패치 검사 결과") as rejected:
        service.prepare(plan, context, source, patch=support.PATCH, patch_meta=meta)

    assert rejected.value.code is ErrorCode.PRECONDITION_FAILED
    assert_no_prepared_patch(patched_release, plan.run_id, previous)


@pytest.mark.parametrize("setting", STATES)
@pytest.mark.parametrize("toggle", STATES)
async def test_checked_patch_reuse_reaches_build_with_toggle_off_or_absent(
    patched_release, setting, toggle
):
    service, source, calls = patched_release
    plan, context = request(service, setting, toggle)
    meta = support.patch_metadata(reuse=True, source="cache")
    review = PatchConfigOutput(
        run_id=plan.run_id,
        status="reused",
        passed=True,
        patch=support.PATCH.decode(),
        patch_sha256=meta["patch_sha256"],
        meta=PatchMeta(reason="fixture", reuse=True, source="cache"),
    )
    run_id = service.prepare(
        plan,
        context,
        source,
        patch=support.PATCH,
        patch_meta=meta,
        patch_review=review,
    )
    view = service.approval_view(run_id)
    assert view["patch_meta"] == meta
    assert view["subjects"]["patch"] == meta["patch_sha256"]
    assert view["snapshot"]["patch_sha256"] == meta["patch_sha256"]
    assert service.store.approvals(run_id) == []
    with pytest.raises(DdakToolError) as unapproved:
        service.start(run_id)
    assert unapproved.value.code is ErrorCode.APPROVAL_REQUIRED
    assert calls.contexts == []

    service.approve(run_id, approver="fixture")
    service.start(run_id)
    assert (await service.wait(run_id)).status is RunStatus.SUCCEEDED
    build_context = next(ctx for name, ctx in calls.contexts if name == "build")
    assert (Path(build_context.build_source) / "app.py").read_bytes() == b"VERSION = 2\n"
    assert (source / "app.py").read_bytes() == b"VERSION = 1\n"
    assert {record.kind for record in service.store.approvals(run_id)} == {"deploy", "patch"}


@pytest.mark.parametrize("setting", STATES)
@pytest.mark.parametrize("toggle", STATES)
@pytest.mark.parametrize(
    "changed_patch",
    [
        pytest.param(support.PATCH.replace(b"+VERSION = 2", b"+VERSION = 3"), id="content"),
        pytest.param(support.PATCH.replace(b"@@\n", b"@@ fixture\n"), id="same-tree-new-bytes"),
    ],
)
async def test_changed_patch_bytes_cannot_reuse_previous_check(
    patched_release, setting, toggle, changed_patch
):
    service, source, _ = patched_release
    plan, context = request(service, setting, toggle)
    previous = service.store.environments("demo")
    meta = support.patch_metadata(reuse=True, source="cache")
    assert support.patch_metadata(changed_patch)["patch_sha256"] != meta["patch_sha256"]

    with pytest.raises(DdakToolError, match="패치 검사 결과") as rejected:
        service.prepare(plan, context, source, patch=changed_patch, patch_meta=meta)

    assert rejected.value.code is ErrorCode.PRECONDITION_FAILED
    assert_no_prepared_patch(patched_release, plan.run_id, previous)


@pytest.mark.parametrize("setting", STATES)
@pytest.mark.parametrize("toggle", STATES)
async def test_no_patch_requires_no_check_metadata(rig, setting, toggle):
    service, source, calls = rig
    plan, context = request(service, setting, toggle)
    run_id = service.prepare(plan, context, source)
    view = service.approval_view(run_id)
    assert view["patch_meta"] is None
    assert set(view["subjects"]) == {"deploy"}
    assert calls.contexts == []


async def test_previous_release_cannot_prepare_partial_patch_without_tool_verdict(patched_release):
    service, source, _ = patched_release
    plan, context = request(service, False, False)
    previous = service.store.environments("demo")
    with pytest.raises(DdakToolError, match="툴 판정"):
        service.prepare(
            plan,
            context,
            source,
            patch=support.PATCH,
            patch_meta=support.patch_metadata(reuse=True, source="cache"),
        )
    assert_no_prepared_patch(patched_release, plan.run_id, previous)
