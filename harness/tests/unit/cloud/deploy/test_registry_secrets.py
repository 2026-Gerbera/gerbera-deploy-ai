import json

import pytest

from ddak.cloud.deploy import registry_secrets
from ddak.cloud.deploy.registry_secrets import seed_registry_secrets
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError
from tests.unit.cloud.test_aws_credentials import SELECTION, StubSessions


class Client:
    def __init__(self, current=()):
        self.calls = []
        self.described = []
        self.current = set(current)

    def describe_secret(self, **kwargs):
        self.described.append(kwargs["SecretId"])
        if kwargs["SecretId"] in self.current:
            return {"VersionIdsToStages": {"v1": ["AWSCURRENT"]}}
        return {}

    def put_secret_value(self, **kwargs):
        self.calls.append(kwargs)


def _ctx():
    return RunContext(
        "run-1",
        adapter_mode=AdapterMode.REAL,
        project_settings=SELECTION,
        platform={
            "cloud": {
                "dockerhub_push_secret_arn": "push-secret",
                "dockerhub_pull_secret_arn": "pull-secret",
            }
        },
    )


def _clear_env(monkeypatch):
    for key in (
        "DDAK_DOCKERHUB_USER",
        "DDAK_DOCKERHUB_PUSH_TOKEN",
        "DDAK_DOCKERHUB_PULL_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


def _client(monkeypatch, client):
    sessions = StubSessions()
    sessions.service = client
    monkeypatch.setattr(registry_secrets.boto3, "Session", sessions)
    return sessions


def test_seed_registry_secrets_uses_host_env_without_returning_values(monkeypatch):
    client = Client()
    monkeypatch.setenv("DDAK_DOCKERHUB_USER", "operator")
    monkeypatch.setenv("DDAK_DOCKERHUB_PUSH_TOKEN", "push-value")
    monkeypatch.setenv("DDAK_DOCKERHUB_PULL_TOKEN", "pull-value")
    sessions = StubSessions()
    sessions.service = client
    monkeypatch.setattr(registry_secrets.boto3, "Session", sessions)
    ctx = RunContext(
        "run-1",
        adapter_mode=AdapterMode.REAL,
        project_settings=SELECTION,
        platform={
            "cloud": {
                "dockerhub_push_secret_arn": "push-secret",
                "dockerhub_pull_secret_arn": "pull-secret",
            }
        },
    )

    assert seed_registry_secrets(ctx) is None
    sessions.sts.get_caller_identity.assert_called_once_with()
    assert sessions.events[:2] == ["sts", "identity"]
    assert [call["SecretId"] for call in client.calls] == ["push-secret", "pull-secret"]
    assert json.loads(client.calls[0]["SecretString"]) == {
        "username": "operator",
        "password": "push-value",
    }
    assert json.loads(client.calls[1]["SecretString"])["password"] == "pull-value"


def test_seed_registry_secrets_requires_all_values_for_empty_secret(monkeypatch):
    _clear_env(monkeypatch)
    client = Client(current={"push-secret"})
    _client(monkeypatch, client)
    with pytest.raises(DdakToolError, match="PUSH_TOKEN") as exc:
        seed_registry_secrets(_ctx())
    assert exc.value.needs_human is True
    assert client.calls == []


def test_seed_registry_secrets_skips_existing_values_without_env(monkeypatch):
    # 기존 플랫폼 재사용: 이미 값이 있는 시크릿은 환경변수 없이도 덮어쓰지 않는다.
    _clear_env(monkeypatch)
    client = Client(current={"push-secret", "pull-secret"})
    _client(monkeypatch, client)
    assert seed_registry_secrets(_ctx()) is None
    assert client.described == ["push-secret", "pull-secret"]
    assert client.calls == []
    monkeypatch.setenv("DDAK_DOCKERHUB_USER", "operator")
    monkeypatch.setenv("DDAK_DOCKERHUB_PUSH_TOKEN", "push-value")
    monkeypatch.setenv("DDAK_DOCKERHUB_PULL_TOKEN", "pull-value")
    assert seed_registry_secrets(_ctx()) is None
    assert client.calls == []


def test_seed_registry_secrets_fills_only_empty_secret(monkeypatch):
    monkeypatch.setenv("DDAK_DOCKERHUB_USER", "operator")
    monkeypatch.setenv("DDAK_DOCKERHUB_PUSH_TOKEN", "push-value")
    monkeypatch.setenv("DDAK_DOCKERHUB_PULL_TOKEN", "pull-value")
    client = Client(current={"push-secret"})
    _client(monkeypatch, client)
    assert seed_registry_secrets(_ctx()) is None
    assert [call["SecretId"] for call in client.calls] == ["pull-secret"]
    assert json.loads(client.calls[0]["SecretString"])["password"] == "pull-value"


def test_seed_registry_secrets_requires_secret_outputs(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(DdakToolError, match="ARN"):
        seed_registry_secrets(
            RunContext("run-1", adapter_mode=AdapterMode.REAL, platform={"cloud": {}})
        )


def test_seed_registry_secrets_is_noop_in_fake_mode(monkeypatch):
    monkeypatch.delenv("DDAK_DOCKERHUB_USER", raising=False)
    assert seed_registry_secrets(RunContext("run-1")) is None
