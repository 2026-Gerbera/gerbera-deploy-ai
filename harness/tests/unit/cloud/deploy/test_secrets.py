"""키별 시크릿 채우기: SECRET_KEY 생성·재사용, 운영자 키는 값 존재만 확인. 네트워크 없음."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from ddak.cloud.deploy.secrets import fill_secrets
from ddak.core.contracts.errors import DdakToolError, ErrorCode

PREFIX = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:ddak/flaskr/"
IDS = {"SECRET_KEY": PREFIX + "SECRET_KEY-AbCdEf", "MAIL_TOKEN": PREFIX + "MAIL_TOKEN-AbCdEf"}
NEW_KEY = "a" * 64
VERSION = "0" * 32


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


def _describe(stub: Stubber, key: str, *, has_value: bool) -> None:
    stages = {VERSION: ["AWSCURRENT"]} if has_value else {}
    stub.add_response(
        "describe_secret", {"Name": key, "VersionIdsToStages": stages}, {"SecretId": IDS[key]}
    )


def test_empty_secret_key_is_generated(client: Any, stub: Stubber) -> None:
    _describe(stub, "SECRET_KEY", has_value=False)
    stub.add_response(
        "put_secret_value",
        {"Name": "SECRET_KEY"},
        {"SecretId": IDS["SECRET_KEY"], "SecretString": NEW_KEY},
    )
    got = fill_secrets(client, ["SECRET_KEY"], IDS, token=lambda: NEW_KEY)
    assert got.generated == ["SECRET_KEY"]
    assert got.changed is True


def test_existing_values_are_reused_without_write(client: Any, stub: Stubber) -> None:
    _describe(stub, "SECRET_KEY", has_value=True)
    _describe(stub, "MAIL_TOKEN", has_value=True)
    stub.add_response(
        "get_secret_value", {"SecretString": "b" * 64}, {"SecretId": IDS["SECRET_KEY"]}
    )
    got = fill_secrets(client, ["SECRET_KEY", "MAIL_TOKEN"], IDS, token=lambda: NEW_KEY)
    assert got.changed is False
    assert got.generated == []


def test_operator_key_value_is_not_read(client: Any, stub: Stubber) -> None:
    _describe(stub, "MAIL_TOKEN", has_value=True)
    got = fill_secrets(client, ["MAIL_TOKEN"], IDS)  # get_secret_value 응답을 넣지 않았다
    assert got.changed is False


def test_missing_operator_key_fails_before_any_write(client: Any, stub: Stubber) -> None:
    _describe(stub, "SECRET_KEY", has_value=False)
    _describe(stub, "MAIL_TOKEN", has_value=False)
    with pytest.raises(DdakToolError) as err:
        fill_secrets(client, ["SECRET_KEY", "MAIL_TOKEN"], IDS, token=lambda: NEW_KEY)
    assert err.value.code is ErrorCode.PRECONDITION_FAILED
    assert "MAIL_TOKEN" in str(err.value)


def test_bad_existing_secret_key_is_not_echoed(client: Any, stub: Stubber) -> None:
    _describe(stub, "SECRET_KEY", has_value=True)
    stub.add_response(
        "get_secret_value", {"SecretString": "d" + "ev-value"}, {"SecretId": IDS["SECRET_KEY"]}
    )
    with pytest.raises(DdakToolError) as err:
        fill_secrets(client, ["SECRET_KEY"], IDS)
    assert err.value.code is ErrorCode.CONFIG_INVALID
    assert "dev-value" not in str(err.value)


def test_missing_output_mapping_is_infra_missing(client: Any) -> None:
    with pytest.raises(DdakToolError) as err:
        fill_secrets(client, ["DB_PASSWORD"], IDS)
    assert err.value.code is ErrorCode.INFRA_MISSING


def test_rejects_bad_key_names(client: Any) -> None:
    with pytest.raises(DdakToolError) as err:
        fill_secrets(client, ["secret_key"], IDS)
    assert err.value.code is ErrorCode.CONFIG_INVALID
