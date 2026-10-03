from unittest.mock import Mock

import pytest

from ddak.cloud.infra import assembly
from ddak.cloud.infra.runtime import SessionKeys
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.tools.generate_infra import GenerateInfraOutput
from ddak.core.snapshots import digest_bytes
from tests.unit.cloud.test_aws_credentials import SELECTION

ACCOUNT = "123456789012"
ACCESS_KEY = "fixture-access-key"
SECRET_KEY = "fixture-" + "long-term-secret-key"


def _create_binding(monkeypatch, tmp_path, *, token=None, credentials_present=True):
    frozen = Mock(access_key=ACCESS_KEY, secret_key=SECRET_KEY, token=token)
    credential = Mock()
    credential.get_frozen_credentials.return_value = frozen
    sts = Mock()
    sts.get_caller_identity.return_value = {"Account": ACCOUNT}

    def make_session(**kwargs):
        session = Mock()
        session.get_credentials.return_value = credential if credentials_present else None
        session.client.return_value = sts
        return session

    session_factory = Mock(side_effect=make_session)
    monkeypatch.setattr(assembly.boto3, "Session", session_factory)
    monkeypatch.setattr(assembly, "InfraRuntime", Mock())

    source = b'resource "aws_s3_bucket" "example" {}\n'
    bundle = GenerateInfraOutput(
        directory=str(tmp_path),
        layer="app",
        files={"main.tf": digest_bytes(source)},
    )
    ctx = RunContext(
        run_id="run-credentials",
        adapter_mode=AdapterMode.REAL,
        project="demo",
        project_settings=SELECTION,
    )
    binding = assembly.create_binding(
        bundle,
        {"main.tf": source.decode()},
        ctx,
        root=tmp_path,
        approvals=lambda: (),
        guard=lambda: None,
    )
    return binding, session_factory, sts


def test_long_term_credentials_are_passed_to_read_and_apply_without_sts_token_request(
    monkeypatch, tmp_path
):
    binding, session_factory, sts = _create_binding(monkeypatch, tmp_path)

    assert binding.read_session() == SessionKeys(ACCESS_KEY, SECRET_KEY, profile_name="g")
    assert binding.apply_session() == SessionKeys(ACCESS_KEY, SECRET_KEY, profile_name="g")
    assert binding.read_session().environment() == {
        "AWS_PROFILE": "g",
    }
    sts.get_session_token.assert_not_called()
    assert session_factory.call_count == 8
    assert sts.get_caller_identity.call_count == 4
    assert all(call.kwargs["profile_name"] == "g" for call in session_factory.call_args_list)
    assert all(
        call.kwargs.get("aws_access_key_id") in (None, ACCESS_KEY)
        for call in session_factory.call_args_list
    )


def test_existing_session_token_is_preserved_for_read_and_apply(monkeypatch, tmp_path):
    token = "fixture-existing-session-token"
    binding, _session_factory, _sts = _create_binding(monkeypatch, tmp_path, token=token)

    assert binding.read_session() == SessionKeys(ACCESS_KEY, SECRET_KEY, token, "g")
    assert binding.apply_session() == SessionKeys(ACCESS_KEY, SECRET_KEY, token, "g")
    assert binding.apply_session().token == token
    assert binding.apply_session().environment() == {"AWS_PROFILE": "g"}


def test_create_binding_rejects_missing_credentials(monkeypatch, tmp_path):
    source = b'resource "aws_s3_bucket" "example" {}\n'
    bundle = GenerateInfraOutput(
        directory=str(tmp_path),
        layer="app",
        files={"main.tf": digest_bytes(source)},
    )
    ctx = RunContext(run_id="run-no-credentials", adapter_mode=AdapterMode.REAL, project="demo")
    monkeypatch.setattr(
        assembly.boto3,
        "Session",
        Mock(return_value=Mock(get_credentials=Mock(return_value=None))),
    )

    with pytest.raises(DdakToolError, match="AWS 자격증명을 확보"):
        assembly.create_binding(
            bundle,
            {"main.tf": source.decode()},
            ctx,
            root=tmp_path,
            approvals=lambda: (),
            guard=lambda: None,
        )


def test_session_keys_reject_empty_access_or_secret_key():
    with pytest.raises(DdakToolError, match="AWS 자격증명 두 값"):
        SessionKeys("", SECRET_KEY).environment()

    with pytest.raises(DdakToolError, match="AWS 자격증명 두 값"):
        SessionKeys(ACCESS_KEY, "").environment()
