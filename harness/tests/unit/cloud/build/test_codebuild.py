"""CodeBuild 호출: 요청 형태(botocore Stubber), 완료 대기, digest 읽기. 네트워크 없음."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from ddak.cloud.build.codebuild import (
    GitSource,
    S3Source,
    check_built_source,
    exported_names,
    read_digests,
    run_build,
    start_build,
    wait_build,
)
from ddak.core.contracts.errors import DdakToolError, ErrorCode

BUILD_ID = "ddak-build:0f1e2d3c"
SHA = "0123456789abcdef0123456789abcdef01234567"
REPO_URL = "https://github.com/gerbera-demo/flaskr"
SOURCE = GitSource(repository_url=REPO_URL, commit_sha=SHA)
S3_SOURCE = S3Source(bucket="ddak-source", key="flaskr/run-1.zip", version_id="v-123", revision=SHA)
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


def _build(
    status: str, exported: list[dict[str, str]] | None = None, resolved: str = SHA
) -> dict[str, Any]:
    build: dict[str, Any] = {
        "id": BUILD_ID,
        "buildStatus": status,
        "resolvedSourceVersion": resolved,
    }
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


OVERRIDES = [
    {"name": "BUILD_TIERS", "value": "was,web", "type": "PLAINTEXT"},
    {"name": "RELEASE_ID", "value": "rel-1", "type": "PLAINTEXT"},
    {"name": "SOURCE_REVISION", "value": SHA, "type": "PLAINTEXT"},
]


def test_start_build_pins_github_commit_and_sends_plaintext_overrides(
    client: Any, stub: Stubber
) -> None:
    stub.add_response(
        "start_build",
        {"build": {"id": BUILD_ID}},
        {
            "projectName": "ddak-build",
            "sourceTypeOverride": "GITHUB",
            "sourceLocationOverride": REPO_URL,
            "sourceVersion": SHA,  # 브랜치 이름이 아니라 커밋 SHA
            "environmentVariablesOverride": OVERRIDES,
        },
    )
    assert start_build(client, "ddak-build", SOURCE, ["was", "web"], "rel-1") == BUILD_ID


def test_start_build_s3_fallback_pins_version_and_sends_revision(
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
            "environmentVariablesOverride": OVERRIDES,
        },
    )
    assert start_build(client, "ddak-build", S3_SOURCE, ["was", "web"], "rel-1") == BUILD_ID


@pytest.mark.parametrize(
    ("tiers", "release_id"),
    [([], "rel-1"), (["was", "was"], "rel-1"), (["Was"], "rel-1"), (["was"], "rel 1;rm")],
)
def test_start_build_rejects_bad_input_before_calling(
    client: Any, stub: Stubber, tiers: list[str], release_id: str
) -> None:
    with pytest.raises(DdakToolError):
        start_build(client, "ddak-build", SOURCE, tiers, release_id)


@pytest.mark.parametrize(
    "source",
    [
        GitSource(repository_url=REPO_URL, commit_sha="ai-prod"),  # 브랜치 이름 금지
        GitSource(repository_url=REPO_URL, commit_sha=SHA[:7]),  # 짧은 SHA 금지
        GitSource(repository_url=REPO_URL, commit_sha=SHA.upper()),
        GitSource(repository_url="https://user:tok" + "en@github.com/o/r", commit_sha=SHA),
        GitSource(repository_url="https://gitlab.com/o/r", commit_sha=SHA),
        GitSource(repository_url="http://github.com/o/r", commit_sha=SHA),
        S3Source(bucket="ddak-source", key="k.zip", version_id="v-1", revision="main"),
    ],
)
def test_start_build_rejects_unpinned_or_bad_source(
    client: Any, stub: Stubber, source: GitSource | S3Source
) -> None:
    with pytest.raises(DdakToolError) as err:
        start_build(client, "ddak-build", source, ["was"], "rel-1")
    assert err.value.code is ErrorCode.CONFIG_INVALID


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
    assert result.revision == SHA
    assert result.digests["was"].index_digest == INDEX
    assert dict(result.digests["was"].platform_digests) == {
        "linux/amd64": AMD64,
        "linux/arm64": ARM64,
    }
    assert clock.now == 5.0


def test_run_build_fails_when_built_commit_differs(client: Any, stub: Stubber) -> None:
    other = "f" * 40
    stub.add_response("start_build", {"build": {"id": BUILD_ID}})
    stub.add_response("batch_get_builds", _build("SUCCEEDED", _exported(), resolved=other))
    with pytest.raises(DdakToolError) as err:
        run_build(client, "ddak-build", SOURCE, ["was"], "rel-1", 100.0, clock=FakeClock())
    assert err.value.code is ErrorCode.ADAPTER_FAILED


def test_check_built_source_uses_version_id_for_s3() -> None:
    check_built_source({"resolvedSourceVersion": "v-123"}, S3_SOURCE)
    with pytest.raises(DdakToolError):
        check_built_source({"resolvedSourceVersion": SHA}, S3_SOURCE)
    with pytest.raises(DdakToolError):
        check_built_source({}, SOURCE)


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
