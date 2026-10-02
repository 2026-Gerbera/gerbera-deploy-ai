import json
from types import SimpleNamespace

import pytest

from ddak.cloud.deploy import registry_secrets
from ddak.cloud.deploy.registry_secrets import seed_registry_secrets
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError


class Client:
    def __init__(self):
        self.calls = []

    def put_secret_value(self, **kwargs):
        self.calls.append(kwargs)


def test_seed_registry_secrets_uses_host_env_without_returning_values(monkeypatch):
    client = Client()
    monkeypatch.setenv("DDAK_DOCKERHUB_USER", "operator")
    monkeypatch.setenv("DDAK_DOCKERHUB_PUSH_TOKEN", "push-value")
    monkeypatch.setenv("DDAK_DOCKERHUB_PULL_TOKEN", "pull-value")
    monkeypatch.setattr(
        registry_secrets.boto3,
        "Session",
        lambda **_kwargs: SimpleNamespace(client=lambda *_args, **_kwargs: client),
    )
    ctx = RunContext(
        "run-1",
        adapter_mode=AdapterMode.REAL,
        platform={
            "cloud": {
                "dockerhub_push_secret_arn": "push-secret",
                "dockerhub_pull_secret_arn": "pull-secret",
            }
        },
    )

    assert seed_registry_secrets(ctx) is None
    assert [call["SecretId"] for call in client.calls] == ["push-secret", "pull-secret"]
    assert json.loads(client.calls[0]["SecretString"]) == {
        "username": "operator",
        "password": "push-value",
    }
    assert json.loads(client.calls[1]["SecretString"])["password"] == "pull-value"


def test_seed_registry_secrets_requires_all_values(monkeypatch):
    for key in (
        "DDAK_DOCKERHUB_USER",
        "DDAK_DOCKERHUB_PUSH_TOKEN",
        "DDAK_DOCKERHUB_PULL_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(DdakToolError, match="PUSH_TOKEN") as exc:
        seed_registry_secrets(
            RunContext("run-1", adapter_mode=AdapterMode.REAL, platform={"cloud": {}})
        )
    assert exc.value.needs_human is True


def test_seed_registry_secrets_is_noop_in_fake_mode(monkeypatch):
    monkeypatch.delenv("DDAK_DOCKERHUB_USER", raising=False)
    assert seed_registry_secrets(RunContext("run-1")) is None
