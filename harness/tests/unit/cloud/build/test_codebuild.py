"""CodeBuild 호출: 요청 형태(botocore Stubber), 완료 대기, digest 읽기. 네트워크 없음."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from ddak.cloud.build.codebuild import (
    BuildSource,
    exported_names,
    read_digests,
    run_build,
    start_build,
    wait_build,
)
from ddak.core.contracts.errors import DdakToolError, ErrorCode

BUILD_ID = "ddak-build:0f1e2d3c"
SOURCE = BuildSource(bucket="ddak-source", key="flaskr/run-1.zip", version_id="v-123")
INDEX = "sha256:" + "1" * 64
AMD64 = "sha256:" + "2" * 64
ARM64 = "sha256:" + "3" * 64


@pytest.fixture
def client() -> Any:
    return boto3.client(
        "codebuild",
        region_name="ap-northeast-2",
        aws_access_key_id="test" + "ing",
        aws_secret_access_key="test" + "ing",
    )


@pytest.fixture
def stub(client: Any) -> Iterator[Stubber]:
    with Stubber(client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def _exported(tier: str = "was") -> list[dict[str, str]]:
    index_name, platform_names = exported_names(tier)
    values = {"linux/amd64": AMD64, "linux/arm64": ARM64}
    return [{"name": index_name, "value": INDEX}] + [
        {"name": name, "value": values[p]} for p, name in platform_names.items()
    ]


def _build(status: str, exported: list[dict[str, str]] | None = None) -> dict[str, Any]:
    build: dict[str, Any] = {"id": BUILD_ID, "buildStatus": status}
    if exported is not None:
        build["exportedEnvironmentVariables"] = exported
    return {"builds": [build]}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_exported_names_follow_tier() -> None:
    assert exported_names("was") == (
        "DDAK_WAS_INDEX",
        {"linux/amd64": "DDAK_WAS_AMD64", "linux/arm64": "DDAK_WAS_ARM64"},
    )
    assert exported_names("api-v2")[0] == "DDAK_API_V2_INDEX"


def test_start_build_sends_pinned_source_and_plaintext_overrides(
    client: Any, stub: Stubber
) -> None:
    stub.add_response(
        "start_build",
        {"build": {"id": BUILD_ID}},
        {
            "projectName": "ddak-build",
            "sourceTypeOverride": "S3",
            "sourceLocationOverride": "ddak-source/flaskr/run-1.zip",
            "sourceVersion": "v-123",
            "environmentVariablesOverride": [
                {"name": "BUILD_TIERS", "value": "was,web", "type": "PLAINTEXT"},
                {"name": "RELEASE_ID", "value": "rel-1", "type": "PLAINTEXT"},
            ],
        },
    )
    assert start_build(client, "ddak-build", SOURCE, ["was", "web"], "rel-1") == BUILD_ID


@pytest.mark.parametrize(
    ("tiers", "release_id"),
    [([], "rel-1"), (["was", "was"], "rel-1"), (["Was"], "rel-1"), (["was"], "rel 1;rm")],
)
def test_start_build_rejects_bad_input_before_calling(
    client: Any, stub: Stubber, tiers: list[str], release_id: str
) -> None:
    with pytest.raises(DdakToolError):
        start_build(client, "ddak-build", SOURCE, tiers, release_id)


def test_start_build_hides_aws_error_detail(client: Any, stub: Stubber) -> None:
    stub.add_client_error("start_build", "AccessDeniedException", "arn:aws:iam::123456789012:x")
    with pytest.raises(DdakToolError) as err:
        start_build(client, "ddak-build", SOURCE, ["was"], "rel-1")
    assert err.value.code is ErrorCode.ADAPTER_FAILED
    assert "123456789012" not in str(err.value)


def test_run_build_polls_until_success_and_reads_digests(client: Any, stub: Stubber) -> None:
    clock = FakeClock()
    stub.add_response("start_build", {"build": {"id": BUILD_ID}})
    stub.add_response("batch_get_builds", _build("IN_PROGRESS"), {"ids": [BUILD_ID]})
    stub.add_response("batch_get_builds", _build("SUCCEEDED", _exported()), {"ids": [BUILD_ID]})
    result = run_build(
        client, "ddak-build", SOURCE, ["was"], "rel-1", 100.0, clock=clock, sleep=clock.sleep
    )
    assert result.build_id == BUILD_ID
    assert result.digests["was"].index_digest == INDEX
    assert dict(result.digests["was"].platform_digests) == {
        "linux/amd64": AMD64,
        "linux/arm64": ARM64,
    }
    assert clock.now == 5.0


@pytest.mark.parametrize("status", ["FAILED", "FAULT", "STOPPED", "TIMED_OUT"])
def test_wait_build_fails_on_terminal_status(client: Any, stub: Stubber, status: str) -> None:
    stub.add_response("batch_get_builds", _build(status))
    with pytest.raises(DdakToolError) as err:
        wait_build(client, BUILD_ID, 100.0, clock=FakeClock())
    assert err.value.code is ErrorCode.ADAPTER_FAILED


def test_wait_build_stops_build_after_deadline(client: Any, stub: Stubber) -> None:
    clock = FakeClock()
    stub.add_response("batch_get_builds", _build("IN_PROGRESS"))
    stub.add_response("batch_get_builds", _build("IN_PROGRESS"))
    stub.add_response("stop_build", {"build": {"id": BUILD_ID}}, {"id": BUILD_ID})
    with pytest.raises(DdakToolError) as err:
        wait_build(client, BUILD_ID, 3.0, poll_s=5.0, clock=clock, sleep=clock.sleep)
    assert err.value.code is ErrorCode.ADAPTER_TIMEOUT
    assert clock.now == 3.0  # deadline을 넘겨 자지 않는다


def test_wait_build_timeout_wins_even_if_stop_fails(client: Any, stub: Stubber) -> None:
    stub.add_response("batch_get_builds", _build("IN_PROGRESS"))
    stub.add_client_error("stop_build", "InvalidInputException")
    with pytest.raises(DdakToolError) as err:
        wait_build(client, BUILD_ID, 0.0, clock=FakeClock())
    assert err.value.code is ErrorCode.ADAPTER_TIMEOUT


def test_read_digests_requires_all_three_per_tier() -> None:
    build = _build("SUCCEEDED", _exported())["builds"][0]
    assert read_digests(build, ["was"])["was"].index_digest == INDEX
    with pytest.raises(DdakToolError):
        read_digests(build, ["web"])  # web은 빌드 결과에 없다
    broken = [v for v in _exported() if not v["name"].endswith("_ARM64")]
    with pytest.raises(DdakToolError):
        read_digests({"exportedEnvironmentVariables": broken}, ["was"])
    tagged = [{**v, "value": "latest"} if v["name"].endswith("_INDEX") else v for v in _exported()]
    with pytest.raises(DdakToolError):
        read_digests({"exportedEnvironmentVariables": tagged}, ["was"])
