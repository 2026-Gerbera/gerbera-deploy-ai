"""실제 SDK/CLI 호출 없이 플랫폼 첫 승인→기반 생성→apply→state 이전을 검사한다."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from ddak.cloud.infra import InfraBinding, bind_infra, run_plan, run_validate, unbind_infra
from ddak.cloud.infra.foundation import foundation_template
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.tools.plan_infra import PlanInfraInput
from ddak.core.contracts.tools.validate_infra import ValidateInfraInput
from ddak.executor.approval_meta import encode_meta
from tests.unit.cloud.infra import test_runtime as f


def missing(code):
    return ClientError({"Error": {"Code": code}}, "fixture")


@pytest.fixture
def bootstrap(tmp_path):
    settings = replace(f.SETTINGS, layer="platform", outputs={})
    sdk = {name: Mock() for name in ("s3", "iam", "sts")}
    sdk["sts"].get_caller_identity.return_value = {"Account": settings.account_id}
    sdk["s3"].head_bucket.side_effect = missing("404")
    sdk["s3"].head_object.side_effect = missing("404")
    sdk["s3"].put_object.return_value = {"ETag": "fixture-etag"}
    sdk["iam"].get_policy.side_effect = missing("NoSuchEntity")
    records = []
    runner = f.FakeRunner()
    runner.raw = {
        "format_version": "1.2",
        "resource_changes": [f.resource("aws_ecs_cluster", "demo", ["create"], {"name": "demo"})],
    }
    runner.outputs = {}
    runtime = f.InfraRuntime(
        root=tmp_path,
        run_id="run-1",
        settings=settings,
        lock_file=b"fixture",
        approvals=lambda: records,
        guard=Mock(),
        runner=runner,
        foundation_clients=lambda session: sdk,
    )
    runtime.prepare_bootstrap(session=f.SESSION)
    assert runtime.validate(
        {"main.tf": 'resource "aws_ecs_cluster" "demo" { name = "demo" }\n'}
    ).passed
    return runtime, runner, sdk, records


def plan(runtime):
    return runtime.plan(session=f.SESSION, analyzer=Mock(), update=False)


def test_first_platform_requires_one_infra_approval_then_creates_and_migrates(bootstrap):
    runtime, runner, sdk, records = bootstrap
    summary = plan(runtime)
    assert encode_meta(summary, infra=True)
    assert summary["iam_diff"][-1]["state_migration"] is True
    assert summary["plan_sha256"] != f.digest(b"fake-saved-plan")
    assert not sdk["s3"].create_bucket.called and not sdk["iam"].create_policy.called
    with pytest.raises(DdakToolError, match="APPROVAL_REQUIRED"):
        runtime.apply(session=f.SESSION)
    assert not sdk["s3"].create_bucket.called
    records.append(f.approval(summary["plan_sha256"]))
    runtime.apply(session=f.SESSION)
    sdk["s3"].create_bucket.assert_called_once()
    assert sdk["iam"].create_policy.call_count == 2
    commands = [cmd for cmd, _ in runner.calls]
    before_apply = commands[: next(i for i, cmd in enumerate(commands) if cmd[1] == "apply")]
    assert not any("-backend-config=backend.json" in cmd for cmd in before_apply)
    assert any("-migrate-state" in cmd and "-force-copy" in cmd for cmd in commands)
    assert json.loads((runtime.work / "ddak.tf.json").read_text())["terraform"]["backend"] == {
        "s3": {}
    }
    assert (runtime.work / "backend-migrated").exists()
    assert (runtime.work / "apply-succeeded").exists()


@pytest.mark.parametrize(
    "change",
    ["plan", "foundation", "bucket-race", "migration", "remote-state", "partial-foundation"],
)
def test_changed_approval_and_partial_bootstrap_do_not_continue(bootstrap, monkeypatch, change):
    runtime, runner, sdk, records = bootstrap
    summary = plan(runtime)
    records.append(f.approval(summary["plan_sha256"]))
    if change == "plan":
        (runtime.work / "approved.tfplan").write_bytes(b"changed")
    elif change == "foundation":
        monkeypatch.setattr("ddak.cloud.infra.foundation.foundation_template", lambda settings: {})
    elif change == "bucket-race":
        sdk["s3"].head_bucket.side_effect = None
    elif change == "partial-foundation":
        sdk["iam"].create_policy.side_effect = missing("AccessDenied")
    elif change == "remote-state":
        sdk["s3"].head_object.side_effect = None
    else:
        original = runner.run

        def run(argv, **kwargs):
            if "-migrate-state" in argv:
                return f.CommandResult(1, "")
            return original(argv, **kwargs)

        runner.run = run
    with pytest.raises(DdakToolError) as exc:
        runtime.apply(session=f.SESSION)
    assert not (runtime.work / "apply-succeeded").exists()
    if change in {"plan", "foundation", "bucket-race"}:
        assert not sdk["s3"].create_bucket.called
        assert not any(cmd[1] == "apply" for cmd, _ in runner.calls)
    else:
        assert exc.value.needs_human
        assert (runtime.work / "apply-started").exists()
        with pytest.raises(DdakToolError):
            runtime.apply(session=f.SESSION)


def test_registered_bootstrap_with_existing_bucket_uses_remote_backend(tmp_path):
    settings = replace(f.SETTINGS, layer="platform", outputs={})
    sdk = {name: Mock() for name in ("s3", "iam", "sts")}
    sdk["sts"].get_caller_identity.return_value = {"Account": settings.account_id}
    template = foundation_template(settings)
    sdk["s3"].get_bucket_tagging.return_value = {
        "TagSet": [{"Key": k, "Value": v} for k, v in template["tags"].items()]
    }
    sdk["s3"].get_bucket_policy.return_value = {"Policy": json.dumps(template["bucket_policy"])}
    sdk["iam"].get_policy.return_value = {"Policy": {"DefaultVersionId": "v1"}}
    sdk["iam"].get_policy_version.side_effect = [
        {"PolicyVersion": {"Document": template[k]}} for k in ("boundary", "build_boundary")
    ]
    records = []
    runner = f.FakeRunner()
    runner.raw = {"format_version": "1.2", "resource_changes": []}
    runner.outputs = {}
    runtime = f.InfraRuntime(
        root=tmp_path,
        run_id="run-1",
        settings=settings,
        lock_file=b"fixture",
        approvals=lambda: records,
        guard=Mock(),
        runner=runner,
        foundation_clients=lambda session: sdk,
    )
    bind_infra(
        InfraBinding(
            runtime,
            {"main.tf": f.HCL},
            AdapterMode.FAKE,
            lambda: f.SESSION,
            lambda: f.SESSION,
            lambda: Mock(),
        )
    )
    ctx = RunContext("run-1", project="flaskr", mode=RunMode.BOOTSTRAP)
    try:
        assert run_validate(ValidateInfraInput(run_id=ctx.run_id), ctx).passed
        summary = run_plan(PlanInfraInput(run_id=ctx.run_id), ctx)
        records.append(f.approval(summary.plan_sha256))
        runtime.apply(session=f.SESSION)
        sdk["s3"].create_bucket.assert_not_called()
        sdk["iam"].create_policy.assert_not_called()
        assert not any("-migrate-state" in cmd for cmd, _ in runner.calls)
        assert any("-backend-config=backend.json" in cmd for cmd, _ in runner.calls)
    finally:
        unbind_infra(ctx.run_id)


def test_bootstrap_plan_orders_infra_before_build_and_tls():
    from ddak.app import _platform_bootstrap_plan
    from ddak.core.contracts.enums import Effect, Layer, Target
    from ddak.core.contracts.plan import PlanStep
    from ddak.executor.engine import check_signals
    from tests.unit.test_deployment_service import plan

    original = plan().model_copy(update={"mode": RunMode.BOOTSTRAP})
    original.deploy.cloud.steps[:0] = [
        PlanStep(
            id="deploy.tls.cloud",
            tool="ensure_tls",
            target=Target.CLOUD,
            layer=Layer.MANDATORY,
            effect=Effect.READ,
        ),
        PlanStep(
            id="deploy.infra.cloud",
            tool="apply_infra",
            target=Target.CLOUD,
            layer=Layer.CONDITIONAL,
            effect=Effect.STATE_CHANGE,
            signal="infra_ready",
        ),
    ]
    ctx = RunContext(original.run_id, project=original.project, mode=RunMode.BOOTSTRAP)
    result = _platform_bootstrap_plan(original, ctx, {"layer": "platform"})
    assert result.deploy.cloud.steps[0].tool == "apply_infra"
    assert result.deploy.cloud.steps[1].tool == "ensure_tls"
    assert all("infra_ready" in s.wait_for for s in result.build.steps)
    assert original.deploy.cloud.steps[0].tool == "ensure_tls"
    assert not any("infra_ready" in s.wait_for for s in original.build.steps)
    check_signals(result)


def test_migration_acquires_shared_lock_before_absence_check(bootstrap):
    runtime, runner, sdk, records = bootstrap
    summary = plan(runtime)
    records.append(f.approval(summary["plan_sha256"]))
    # 다른 실행이 잠금을 먼저 확보한 경우 대상 검사/이전은 시작하지 않는다.
    sdk["s3"].put_object.side_effect = missing("PreconditionFailed")
    with pytest.raises(DdakToolError) as exc:
        runtime.apply(session=f.SESSION)
    assert exc.value.needs_human
    sdk["s3"].head_object.assert_not_called()
    sdk["s3"].delete_object.assert_not_called()
    assert not any("-migrate-state" in cmd for cmd, _ in runner.calls)


def test_migration_lock_released_only_after_success(bootstrap):
    runtime, _runner, sdk, records = bootstrap
    summary = plan(runtime)
    records.append(f.approval(summary["plan_sha256"]))
    runtime.apply(session=f.SESSION)
    methods = [c[0] for c in sdk["s3"].mock_calls]
    assert (
        methods.index("put_object") < methods.index("head_object") < methods.index("delete_object")
    )
    assert sdk["s3"].put_object.call_args.kwargs["IfNoneMatch"] == "*"
    assert sdk["s3"].delete_object.call_args.kwargs["IfMatch"] == "fixture-etag"


def test_partial_local_state_blocks_a_different_run_until_recovery(bootstrap, tmp_path):
    runtime, runner, _sdk, records = bootstrap
    records.append(f.approval(plan(runtime)["plan_sha256"]))
    runner.apply_code = 1
    with pytest.raises(DdakToolError) as exc:
        runtime.apply(session=f.SESSION)
    assert exc.value.needs_human
    marker = runtime._bootstrap_attempt
    proof = json.loads(marker.read_text())
    assert proof["local_state_path"] == str(runtime.work / "terraform.tfstate")
    assert str(marker) in str(exc.value)
    with pytest.raises(DdakToolError, match="로컬 state 복구"):
        f.InfraRuntime(
            root=tmp_path,
            run_id="run-new",
            settings=runtime.settings,
            lock_file=b"fixture",
            approvals=lambda: [],
            guard=Mock(),
            runner=runner,
        )
    assert marker.exists()


def test_success_clears_bootstrap_marker_and_migration_has_separate_budget(bootstrap):
    import time

    runtime, runner, _sdk, records = bootstrap
    records.append(f.approval(plan(runtime)["plan_sha256"]))
    deadlines = []
    original = runner.run

    def run(argv, **kwargs):
        if argv[1] == "apply":
            runtime.deadline = time.monotonic() - 1
        if "-migrate-state" in argv:
            deadlines.append(kwargs["deadline"] - time.monotonic())
        return original(argv, **kwargs)

    runner.run = run
    runtime.migration_timeout = 17
    runtime.apply(session=f.SESSION)
    assert not runtime._bootstrap_attempt.exists()
    assert len(deadlines) == 1 and 15 < deadlines[0] <= 17


def test_tagging_failure_keeps_bucket_creation_evidence_without_deletion(bootstrap):
    runtime, runner, sdk, records = bootstrap
    records.append(f.approval(plan(runtime)["plan_sha256"]))
    sdk["s3"].put_bucket_tagging.side_effect = missing("AccessDenied")
    with pytest.raises(DdakToolError) as exc:
        runtime.apply(session=f.SESSION)
    assert exc.value.needs_human
    evidence = list(runtime.work.parent.glob("*-foundation-bucket-created.json"))
    assert len(evidence) == 1
    assert json.loads(evidence[0].read_text())["bucket"] == runtime.settings.state_bucket
    sdk["s3"].delete_bucket.assert_not_called()
    sdk["s3"].put_public_access_block.assert_not_called()
    assert not any(cmd[1] == "apply" for cmd, _ in runner.calls)
