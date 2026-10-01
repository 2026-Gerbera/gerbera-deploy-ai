"""빌드 소스 업로드: 결정적 zip, 제외 규칙, S3 호출 형태(Stubber), 실패 경로. 네트워크 없음."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber

from ddak.cloud.build.codebuild import BuildSource
from ddak.cloud.build.source import source_key, upload_source, zip_source
from ddak.core.contracts.errors import DdakToolError, ErrorCode

BUCKET = "ddak-source"
KEY = "sources/flaskr/run-1.zip"


@pytest.fixture
def app(tmp_path: Path) -> Path:
    root = tmp_path / "build-source"
    (root / "flaskr").mkdir(parents=True)
    (root / "docker").mkdir()
    (root / "flaskr" / "__init__.py").write_text("VERSION = 2\n")
    (root / "docker" / "was.Dockerfile").write_text("FROM python:3.13-slim\n")
    entry = root / "docker" / "entrypoint.sh"
    entry.write_text("#!/bin/sh\nexec gunicorn\n")
    entry.chmod(0o755)
    (root / ".env").write_text("SECRET_KEY=" + "dev\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return root


@pytest.fixture
def client() -> Any:
    return boto3.client(
        "s3",
        region_name="ap-northeast-2",
        aws_access_key_id="test" + "ing",
        aws_secret_access_key="test" + "ing",
    )


@pytest.fixture
def stub(client: Any) -> Iterator[Stubber]:
    with Stubber(client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def _entries(body: bytes) -> dict[str, zipfile.ZipInfo]:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return {info.filename: info for info in archive.infolist()}


def test_zip_has_build_files_only_in_name_order(app: Path) -> None:
    entries = _entries(zip_source(app))

    assert list(entries) == [
        "docker/entrypoint.sh",
        "docker/was.Dockerfile",
        "flaskr/__init__.py",
    ]  # .env·.git은 승인 해시와 같은 규칙으로 빠진다


def test_zip_keeps_exec_bit_and_drops_mtime(app: Path) -> None:
    entries = _entries(zip_source(app))

    assert entries["docker/entrypoint.sh"].external_attr >> 16 == 0o100755
    assert entries["flaskr/__init__.py"].external_attr >> 16 == 0o100644
    assert {info.date_time for info in entries.values()} == {(1980, 1, 1, 0, 0, 0)}


def test_same_content_gives_same_zip(app: Path) -> None:
    first = zip_source(app)
    os.utime(app / "flaskr" / "__init__.py", (1_000_000_000, 1_000_000_000))

    assert zip_source(app) == first


def test_changed_content_gives_different_zip(app: Path) -> None:
    first = zip_source(app)
    (app / "flaskr" / "__init__.py").write_text("VERSION = 3\n")

    assert zip_source(app) != first


def test_zip_rejects_symlink(app: Path) -> None:
    (app / "link").symlink_to(app / "flaskr" / "__init__.py")

    with pytest.raises(DdakToolError) as err:
        zip_source(app)
    assert err.value.code is ErrorCode.PRECONDITION_FAILED


def test_zip_rejects_empty_source(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("A=" + "b\n")

    with pytest.raises(DdakToolError) as err:
        zip_source(tmp_path)
    assert err.value.code is ErrorCode.PRECONDITION_FAILED


def test_upload_pins_version_and_sends_checksum(client: Any, stub: Stubber, app: Path) -> None:
    body = zip_source(app)
    checksum = base64.b64encode(hashlib.sha256(body).digest()).decode()
    stub.add_response(
        "put_object",
        {"VersionId": "v-123"},
        {
            "Bucket": BUCKET,
            "Key": KEY,
            "Body": ANY,
            "ContentType": "application/zip",
            "ChecksumSHA256": checksum,
        },
    )

    assert upload_source(client, BUCKET, KEY, app) == BuildSource(
        bucket=BUCKET, key=KEY, version_id="v-123"
    )


@pytest.mark.parametrize("response", [{}, {"VersionId": "null"}])
def test_upload_fails_without_bucket_versioning(
    client: Any, stub: Stubber, app: Path, response: dict[str, str]
) -> None:
    stub.add_response("put_object", response)

    with pytest.raises(DdakToolError) as err:
        upload_source(client, BUCKET, KEY, app)
    assert err.value.code is ErrorCode.PRECONDITION_FAILED


def test_upload_hides_client_error_detail(client: Any, stub: Stubber, app: Path) -> None:
    stub.add_client_error(
        "put_object",
        service_error_code="AccessDenied",
        service_message="arn:aws:iam::123456789012:role/deployer is not authorized",
    )

    with pytest.raises(DdakToolError) as err:
        upload_source(client, BUCKET, KEY, app)
    assert err.value.code is ErrorCode.ADAPTER_FAILED
    assert "123456789012" not in str(err.value)


def test_upload_rejects_bad_bucket_before_call(client: Any, stub: Stubber, app: Path) -> None:
    with pytest.raises(DdakToolError) as err:
        upload_source(client, "Bad_Bucket", KEY, app)
    assert err.value.code is ErrorCode.CONFIG_INVALID


def test_source_key_layout() -> None:
    assert source_key("flaskr", "run-1") == KEY


@pytest.mark.parametrize(("project", "run_id"), [("..", "run-1"), ("flaskr", "a/b"), ("", "r")])
def test_source_key_rejects_path_like_names(project: str, run_id: str) -> None:
    with pytest.raises(DdakToolError) as err:
        source_key(project, run_id)
    assert err.value.code is ErrorCode.CONFIG_INVALID
