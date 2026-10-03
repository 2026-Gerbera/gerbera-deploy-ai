"""모든 AWS/Terraform 경계는 stub. 실제 프로필·자격증명 파일·네트워크를 사용하지 않는다."""

from __future__ import annotations

import os
import time
import traceback
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ddak.cloud.build import image
from ddak.cloud.deploy import _aws, registry_secrets
from ddak.cloud.health import aws as health
from ddak.cloud.infra import assembly
from ddak.cloud.infra.runtime import AwsSettings, CommandRunner, InfraRuntime, SessionKeys
from ddak.cloud.tls import check as tls
from ddak.core import aws_credentials
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.generate_infra import GenerateInfraOutput
from ddak.core.snapshots import digest_bytes

ACCOUNT = "123456789012"
SELECTION = {"aws_profile": "g", "aws_expected_account_id": ACCOUNT}


class StubSessions:
    def __init__(self, account=ACCOUNT):
        self.account = account
        self.failure = None
        self.calls = []
        self.events = []
        self.service = Mock()
        self.sts = Mock(get_caller_identity=Mock(side_effect=self.identity))
        self.access = "fixture-access"
        self.secret = "fixture-" + "secret"
        self.token = "fixture-session"

    def identity(self):
        self.events.append("identity")
        if self.failure is not None:
            raise self.failure
        return {"Account": self.account}

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        frozen = SimpleNamespace(
            access_key=kwargs.get("aws_access_key_id", self.access),
            secret_key=kwargs.get("aws_secret_access_key", self.secret),
            token=kwargs.get("aws_session_token", self.token),
        )

        def client(name, **_kwargs):
            self.events.append(name)
            return self.sts if name == "sts" else self.service

        return SimpleNamespace(
            client=client,
            get_credentials=lambda: SimpleNamespace(get_frozen_credentials=lambda: frozen),
        )


@pytest.fixture
def sdk(monkeypatch):
    stub = StubSessions()
    monkeypatch.setattr(aws_credentials.boto3, "Session", stub)
    return stub


def context(selection=SELECTION):
    return RunContext(
        "aws-guard",
        project="fixture",
        adapter_mode=AdapterMode.REAL,
        project_settings=selection,
        cloud_domain="app.example.test",
        platform={
            "cloud": {
                "alb_arn": "alb-fixture",
                "certificate_arn": "cert-fixture",
                "https_listener_arn": "https-fixture",
                "http_listener_arn": "http-fixture",
                "dockerhub_push_secret_arn": "push-fixture",
                "dockerhub_pull_secret_arn": "pull-fixture",
            }
        },
    )


def runtime(tmp_path, *, runner=None):
    return InfraRuntime(
        root=tmp_path,
        run_id="aws-guard",
        settings=AwsSettings("fixture", ACCOUNT, "fixture-state", "app", {}),
        aws_project_settings=SELECTION,
        lock_file=b"fixture-lock",
        approvals=lambda: (),
        guard=lambda: None,
        runner=runner or Mock(),
    )


PATHS = ("build", "deploy", "registry", "tls", "health", "assembly", "infra-clients")


def invoke(path, ctx, monkeypatch, tmp_path):
    if path == "build":
        return image._codebuild({}, ctx)
    if path == "deploy":
        return _aws.client("ecs", ctx)
    if path == "registry":
        monkeypatch.setenv("DDAK_DOCKERHUB_USER", "fixture-user")
        monkeypatch.setenv("DDAK_DOCKERHUB_PUSH_TOKEN", "fixture-push")
        monkeypatch.setenv("DDAK_DOCKERHUB_PULL_TOKEN", "fixture-pull")
        return registry_secrets.seed_registry_secrets(ctx)
    if path == "tls":
        monkeypatch.setattr(tls, "check_tls", Mock())
        return tls.ensure_tls("check", ctx)
    if path == "health":
        return health.client("ecs", ctx, "ap-northeast-2")
    if path == "infra-clients":
        return runtime(tmp_path)._clients(SessionKeys("fixture-access", "fixture-value"))
    source = 'resource "aws_s3_bucket" "fixture" {}'
    bundle = GenerateInfraOutput(
        directory=str(tmp_path), layer="app", files={"main.tf": digest_bytes(source.encode())}
    )
    return assembly.create_binding(
        bundle,
        {"main.tf": source},
        ctx,
        root=tmp_path,
        approvals=lambda: (),
        guard=lambda: None,
    )


@pytest.mark.parametrize("path", PATHS)
def test_selected_profile_overrides_environment_and_readonly(path, sdk, monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_PROFILE", "ambient-other")
    monkeypatch.setenv("DDAK_AWS_READONLY_PROFILE", "readonly-other")
    invoke(path, context(), monkeypatch, tmp_path)
    assert sdk.calls and all(call["profile_name"] == "g" for call in sdk.calls)
    assert sdk.events[:2] == ["sts", "identity"]
    assert sdk.sts.get_caller_identity.call_count >= 1
    assert os.environ["AWS_PROFILE"] == "ambient-other"
    assert os.environ["DDAK_AWS_READONLY_PROFILE"] == "readonly-other"
    if path == "registry":
        assert sdk.service.put_secret_value.call_count == 2


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("failure", ["mismatch", "lookup", "malformed"])
def test_account_failure_blocks_service_clients_and_hides_values(
    path, failure, sdk, monkeypatch, tmp_path
):
    sdk.account = "999999999999" if failure == "mismatch" else None
    if failure == "lookup":
        sdk.failure = RuntimeError("fixture-private-value profile=g account=999999999999")
    with pytest.raises(DdakToolError) as caught:
        invoke(path, context(), monkeypatch, tmp_path)
    assert caught.value.code is (
        ErrorCode.PRECONDITION_FAILED if failure == "mismatch" else ErrorCode.CONFIG_INVALID
    )
    assert caught.value.needs_human
    assert sdk.events == ["sts", "identity"]
    sdk.service.assert_not_called()
    assert not sdk.service.mock_calls
    formatted = "".join(traceback.format_exception(caught.value))
    assert all(
        value not in formatted for value in (ACCOUNT, "999999999999", "fixture-private-value")
    )


@pytest.mark.parametrize("selection", [{}, {"aws_profile": None, "aws_expected_account_id": None}])
def test_missing_snapshot_falls_back_to_checked_in_file(selection, sdk):
    sdk.account = "458781646776"
    aws_credentials.checked_session(selection)
    assert {call["profile_name"] for call in sdk.calls} == {"g"}
    sdk.sts.get_caller_identity.assert_called_once_with()


@pytest.mark.parametrize("field", ["aws_profile", "aws_expected_account_id"])
@pytest.mark.parametrize("value", ["", " ", "bad\nvalue", 7])
def test_invalid_snapshot_fails_closed_without_session(field, value, sdk):
    with pytest.raises(DdakToolError, match="AWS 프로필과 기대 계정"):
        aws_credentials.checked_session({**SELECTION, field: value})
    assert not sdk.calls


@pytest.mark.parametrize("missing", ["profile", "account", "file"])
def test_bad_file_defaults_fail_closed(missing, sdk, monkeypatch):
    monkeypatch.setattr(
        aws_credentials.defaults,
        "load_defaults",
        lambda: {} if missing == "profile" else {"aws_profile": "g"},
    )
    loader = Mock(return_value={} if missing == "account" else {"expected_account_id": ACCOUNT})
    if missing == "file":
        loader.side_effect = OSError("fixture-private-path")
    monkeypatch.setattr(aws_credentials.defaults, "load_aws_defaults", loader)
    with pytest.raises(DdakToolError) as caught:
        aws_credentials.checked_session({})
    assert "fixture-private-path" not in str(caught.value)
    assert not sdk.calls


def test_different_projects_do_not_change_global_profile(sdk, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "ambient")
    for profile in ("g", "other-project", "g"):
        aws_credentials.checked_session({**SELECTION, "aws_profile": profile})
    assert [call["profile_name"] for call in sdk.calls] == [
        "g",
        "g",
        "other-project",
        "other-project",
        "g",
        "g",
    ]
    assert os.environ["AWS_PROFILE"] == "ambient"


def test_profile_credentials_are_frozen_before_sts_and_service(sdk):
    session = aws_credentials.checked_session(SELECTION)
    assert len(sdk.calls) == 2
    assert "aws_access_key_id" not in sdk.calls[0]
    assert sdk.calls[1]["aws_access_key_id"] == "fixture-access"
    sdk.access = "rotated-to-another-account"
    assert session.get_credentials().get_frozen_credentials().access_key == "fixture-access"
    session.client("ecs").describe_clusters()
    assert sdk.events == ["sts", "identity", "ecs"]


@pytest.mark.parametrize("verb", ["init", "plan", "apply", "output"])
@pytest.mark.parametrize("failure", ["mismatch", "lookup"])
def test_runtime_rechecks_explicit_keys_before_every_terraform_command(
    verb, failure, sdk, tmp_path
):
    runner = Mock()
    rt = runtime(tmp_path, runner=runner)
    sdk.account = "999999999999"
    if failure == "lookup":
        sdk.failure = RuntimeError("fixture-private-value")
    with pytest.raises(DdakToolError):
        rt._run(verb, session=SessionKeys("fixed-access", "fixed-value", "fixed-token"))
    runner.run.assert_not_called()
    assert sdk.calls == [
        {
            "profile_name": "g",
            "region_name": "ap-northeast-2",
            "aws_access_key_id": "fixed-access",
            "aws_secret_access_key": "fixed-value",
            "aws_session_token": "fixed-token",
        }
    ]


def test_validate_mismatch_prevents_even_offline_terraform(sdk, tmp_path):
    from tests.unit.cloud.infra.test_runtime import HCL

    runner = Mock()
    rt = runtime(tmp_path, runner=runner)
    sdk.account = "999999999999"
    with pytest.raises(DdakToolError):
        rt.validate({"main.tf": HCL})
    runner.run.assert_not_called()


def test_plan_to_apply_account_change_does_not_start_apply(sdk, tmp_path):
    from tests.unit.cloud.infra.test_runtime import HCL, SETTINGS, FakeRunner, approval

    runner, approvals = FakeRunner(), []
    rt = InfraRuntime(
        root=tmp_path,
        run_id="run-1",
        settings=SETTINGS,
        aws_project_settings=SELECTION,
        lock_file=b"fixture-lock",
        approvals=lambda: approvals,
        guard=lambda: None,
        runner=runner,
    )
    assert rt.validate({"main.tf": HCL}).passed
    keys = SessionKeys("fixed-access", "fixed-value", "fixed-token", "g")
    summary = rt.plan(
        session=keys, analyzer=Mock(validate_policy=Mock(return_value={"findings": []}))
    )
    approvals.append(approval(summary["plan_sha256"]))
    before = list(runner.calls)
    sdk.account = "999999999999"
    with pytest.raises(DdakToolError, match="계정이 기대 계정과"):
        rt.apply(session=replace(keys, access_key="changed-access"))
    assert runner.calls == before
    assert not (rt.work / "apply-started").exists()
    assert not rt._attempt.exists()


def test_provider_account_allowlist_uses_expected_account(sdk, monkeypatch, tmp_path):
    binding = invoke("assembly", context(), monkeypatch, tmp_path)
    framework = binding.runtime.settings.framework()
    assert framework["provider"]["aws"]["allowed_account_ids"] == [ACCOUNT]
    assert binding.runtime.settings.account_id == ACCOUNT


def test_runtime_rejects_different_declared_account_before_workdir(tmp_path):
    with pytest.raises(DdakToolError, match="기대 계정"):
        InfraRuntime(
            root=tmp_path / "not-created",
            run_id="fixture",
            settings=AwsSettings("fixture", "999999999999", "fixture-state", "app", {}),
            aws_project_settings=SELECTION,
            lock_file=b"fixture",
            approvals=lambda: (),
            guard=lambda: None,
        )
    assert not (tmp_path / "not-created").exists()


@pytest.mark.parametrize("custom_paths", [False, True])
def test_terraform_resolves_selected_profile_without_ambient_keys(
    tmp_path, monkeypatch, custom_paths
):
    import ddak.cloud.infra.runtime as module

    captured = {}

    def popen(_argv, **kwargs):
        env = kwargs["env"]
        captured.update(env)
        assert Path(env["TF_CLI_CONFIG_FILE"]).parent != Path(env["HOME"])
        assert Path(env["TF_CLI_CONFIG_FILE"]).read_text() == "disable_checkpoint = true\n"
        return Mock(returncode=0, wait=Mock(return_value=0))

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setenv("AWS_PROFILE", "ambient")
    monkeypatch.setenv("AWS_DEFAULT_PROFILE", "ambient-default")
    monkeypatch.setenv("HOME", str(tmp_path / "operator-home"))
    for name in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        if custom_paths:
            monkeypatch.setenv(name, str(tmp_path / name.lower()))
        else:
            monkeypatch.delenv(name, raising=False)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "ambient-value")
    CommandRunner().run(
        ["terraform-stub", "plan"],
        cwd=tmp_path,
        deadline=time.monotonic() + 10,
        session=SessionKeys("fixed-access", "fixed-value", "fixed-token", "g"),
    )
    assert captured["AWS_PROFILE"] == captured["AWS_DEFAULT_PROFILE"] == "g"
    assert captured["HOME"] == str(tmp_path / "operator-home")
    assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"} & captured.keys()
    for name in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        assert captured.get(name) == (str(tmp_path / name.lower()) if custom_paths else None)
    assert not Path(captured["TF_CLI_CONFIG_FILE"]).exists()
    assert os.environ["AWS_PROFILE"] == "ambient"


def test_terraform_profile_mode_rechecks_resolved_profile_not_old_session_keys(sdk, tmp_path):
    runner = Mock()
    rt = runtime(tmp_path, runner=runner)
    stale = SessionKeys("stale-access", "stale-value", "stale-token", "g")
    rt._run("plan", session=stale)
    assert sdk.calls[0] == {"profile_name": "g", "region_name": "ap-northeast-2"}
    assert sdk.calls[1]["aws_access_key_id"] == sdk.access
    assert runner.run.call_args.kwargs["session"].environment() == {"AWS_PROFILE": "g"}
    sdk.account = "999999999999"
    runner.reset_mock()
    with pytest.raises(DdakToolError):
        rt._run("apply", session=stale)
    runner.run.assert_not_called()
