"""시크릿 값 채우기: SECRET_KEY 생성·재사용, 운영자 키 확인, 값이 오류에 안 나옴. 네트워크 없음."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from ddak.cloud.deploy.secrets import fill_secret
from ddak.core.contracts.errors import DdakToolError, ErrorCode

SECRET = "ddak/flaskr/app"
NEW_KEY = "a" * 64
DB_PASSWORD = "pw-" + "operator"  # 가짜 값(이어 붙여 gitleaks 회피)


@pytest.fixture
def client() -> Any:
    return boto3.client(
        "secretsmanager",
        region_name="ap-northeast-2",
        aws_access_key_id="test" + "ing",
        aws_secret_access_key="test" + "ing",
    )


@pytest.fixture
def stub(client: Any) -> Iterator[Stubber]:
    with Stubber(client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def _describe(stub: Stubber, *, has_value: bool) -> None:
    stages = {"0" * 32: ["AWSCURRENT"]} if has_value else {}
    stub.add_response(
        "describe_secret", {"Name": SECRET, "VersionIdsToStages": stages}, {"SecretId": SECRET}
    )


def _value(stub: Stubber, values: dict[str, str] | str) -> None:
    body = values if isinstance(values, str) else json.dumps(values)
    stub.add_response("get_secret_value", {"SecretString": body}, {"SecretId": SECRET})


def test_empty_secret_gets_generated_key(client: Any, stub: Stubber) -> None:
    _describe(stub, has_value=False)
    stub.add_response(
        "put_secret_value",
        {"Name": SECRET},
        {"SecretId": SECRET, "SecretString": json.dumps({"SECRET_KEY": NEW_KEY})},
    )
    got = fill_secret(client, SECRET, ["SECRET_KEY"], token=lambda: NEW_KEY)
    assert got.generated == ["SECRET_KEY"]
    assert got.changed is True


def test_existing_values_are_reused_without_write(client: Any, stub: Stubber) -> None:
    _describe(stub, has_value=True)
    _value(stub, {"SECRET_KEY": "b" * 64, "DB_PASSWORD": DB_PASSWORD})
    got = fill_secret(client, SECRET, ["SECRET_KEY", "DB_PASSWORD"], token=lambda: NEW_KEY)
    assert got.changed is False
    assert got.generated == []


def test_new_key_keeps_operator_values(client: Any, stub: Stubber) -> None:
    _describe(stub, has_value=True)
    _value(stub, {"DB_PASSWORD": DB_PASSWORD})
    body = json.dumps({"DB_PASSWORD": DB_PASSWORD, "SECRET_KEY": NEW_KEY}, sort_keys=True)
    stub.add_response(
        "put_secret_value", {"Name": SECRET}, {"SecretId": SECRET, "SecretString": body}
    )
    got = fill_secret(client, SECRET, ["SECRET_KEY", "DB_PASSWORD"], token=lambda: NEW_KEY)
    assert got.generated == ["SECRET_KEY"]


def test_missing_operator_key_fails(client: Any, stub: Stubber) -> None:
    _describe(stub, has_value=False)
    with pytest.raises(DdakToolError) as err:
        fill_secret(client, SECRET, ["DB_PASSWORD"])
    assert err.value.code is ErrorCode.PRECONDITION_FAILED
    assert "DB_PASSWORD" in str(err.value)


def test_bad_existing_secret_key_fails(client: Any, stub: Stubber) -> None:
    _describe(stub, has_value=True)
    _value(stub, {"SECRET_KEY": "dev"})
    with pytest.raises(DdakToolError) as err:
        fill_secret(client, SECRET, ["SECRET_KEY"])
    assert err.value.code is ErrorCode.CONFIG_INVALID


def test_non_json_value_is_not_echoed(client: Any, stub: Stubber) -> None:
    _describe(stub, has_value=True)
    _value(stub, "plain-" + "secret-text")
    with pytest.raises(DdakToolError) as err:
        fill_secret(client, SECRET, ["SECRET_KEY"])
    assert err.value.code is ErrorCode.CONFIG_INVALID
    assert "secret-text" not in str(err.value)


def test_rejects_bad_key_names(client: Any) -> None:
    with pytest.raises(DdakToolError) as err:
        fill_secret(client, SECRET, ["secret_key"])
    assert err.value.code is ErrorCode.CONFIG_INVALID
