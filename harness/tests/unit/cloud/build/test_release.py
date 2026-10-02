"""빌드 한 번 → ReleaseArtifacts: 가짜 CodeBuild로 실제 코드 경로를 통과시킨다. 네트워크 없음."""

from __future__ import annotations

import pytest

from ddak.cloud.build.codebuild import GitSource, S3Source
from ddak.cloud.build.fake import FakeCodeBuild, fake_digest
from ddak.cloud.build.release import build_release, build_tier
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.release import ImageArtifact, ReleaseArtifacts, SnapshotBinding

SHA = "0123456789abcdef0123456789abcdef01234567"
SOURCE = GitSource(repository_url="https://github.com/gerbera-demo/flaskr", commit_sha=SHA)
RID = "run-20261002-054700-ab12"
IMAGE_REPO = "docker.io/gerbera/ddak"
SNAPSHOT = SnapshotBinding(
    source_snapshot_hash="sha256:" + "a" * 64, build_snapshot_hash="sha256:" + "a" * 64
)
OLD_WEB = ImageArtifact(
    ref="docker.io/gerbera/ddak@sha256:" + "4" * 64,
    index_digest="sha256:" + "4" * 64,
    platform_digests={"linux/amd64": "sha256:" + "5" * 64, "linux/arm64": "sha256:" + "6" * 64},
)


def _build(client: FakeCodeBuild, tiers: list[str], **kwargs: object):
    args: dict[str, object] = {
        "project": "ddak-build",
        "source": SOURCE,
        "tiers": tiers,
        "release_id": RID,
        "image_repo": IMAGE_REPO,
        "snapshot": SNAPSHOT,
        "deadline": 100.0,
        "clock": lambda: 0.0,
        "sleep": lambda _s: None,
    }
    args.update(kwargs)
    return build_release(client, **args)  # type: ignore[arg-type]


def test_builds_all_tiers_in_one_codebuild_run() -> None:
    client = FakeCodeBuild()
    got = _build(client, ["web", "was"])

    assert len(client.started) == 1
    assert got.build_id == "ddak-build:fake-1"
    assert got.revision == SHA
    assert got.artifacts.snapshot == SNAPSHOT
    was = got.artifacts.images["was"]
    assert was.index_digest == fake_digest("was", SHA, "index")
    assert was.ref == f"docker.io/gerbera/ddak@{was.index_digest}"
    assert was.platform_digests["linux/arm64"] == fake_digest("was", SHA, "linux/arm64")


def test_unchanged_tier_keeps_previous_image() -> None:
    client = FakeCodeBuild()
    got = _build(client, ["was"], unchanged={"web": OLD_WEB})

    env = {v["name"]: v["value"] for v in client.started[0]["environmentVariablesOverride"]}
    assert env["BUILD_TIERS"] == "was"
    assert got.artifacts.images["web"] == OLD_WEB
    assert set(got.artifacts.images) == {"web", "was"}


def test_nothing_changed_skips_codebuild() -> None:
    client = FakeCodeBuild()
    got = _build(client, [], unchanged={"web": OLD_WEB})

    assert client.started == []
    assert got.build_id is None
    assert got.artifacts.images == {"web": OLD_WEB}


def test_rejects_tier_both_built_and_kept() -> None:
    with pytest.raises(DdakToolError) as exc:
        _build(FakeCodeBuild(), ["web"], unchanged={"web": OLD_WEB})
    assert exc.value.code is ErrorCode.CONFIG_INVALID


def test_rejects_empty_release() -> None:
    with pytest.raises(DdakToolError) as exc:
        _build(FakeCodeBuild(), [])
    assert exc.value.code is ErrorCode.PRECONDITION_FAILED


def test_failed_build_raises() -> None:
    with pytest.raises(DdakToolError) as exc:
        _build(FakeCodeBuild(fail_status="FAILED"), ["web"])
    assert exc.value.code is ErrorCode.ADAPTER_FAILED


def test_same_commit_gives_same_digests() -> None:
    a = _build(FakeCodeBuild(), ["web"]).artifacts
    b = _build(FakeCodeBuild(), ["web"]).artifacts
    assert a == b


def _tier(client: FakeCodeBuild, tier: str, current: object, **kwargs: object):
    args: dict[str, object] = {
        "tier": tier,
        "current": current,
        "project": "ddak-build",
        "source": SOURCE,
        "release_id": RID,
        "image_repo": IMAGE_REPO,
        "snapshot": SNAPSHOT,
        "deadline": 100.0,
        "clock": lambda: 0.0,
        "sleep": lambda _s: None,
    }
    args.update(kwargs)
    return build_tier(client, **args)  # type: ignore[arg-type]


def test_build_steps_accumulate_tiers_in_one_release() -> None:
    client = FakeCodeBuild()
    first = _tier(client, "web", None)
    second = _tier(client, "was", first.artifacts)

    assert len(client.started) == 2  # step마다 CodeBuild 한 번
    assert set(second.artifacts.images) == {"web", "was"}
    assert second.artifacts.images["web"] == first.artifacts.images["web"]


def test_rebuilding_same_tier_replaces_it() -> None:
    client = FakeCodeBuild()
    stale = ReleaseArtifacts(snapshot=SNAPSHOT, images={"web": OLD_WEB})
    got = _tier(client, "web", stale)
    assert got.artifacts.images["web"] != OLD_WEB
    assert set(got.artifacts.images) == {"web"}


def test_rejects_previous_result_from_other_snapshot() -> None:
    other = SnapshotBinding(
        source_snapshot_hash="sha256:" + "b" * 64, build_snapshot_hash="sha256:" + "b" * 64
    )
    current = ReleaseArtifacts(snapshot=other, images={"web": OLD_WEB})
    client = FakeCodeBuild()
    with pytest.raises(DdakToolError) as exc:
        _tier(client, "was", current)
    assert exc.value.code is ErrorCode.PRECONDITION_FAILED
    assert client.started == []


def test_same_repo_value_drives_push_and_artifact_ref() -> None:
    client = FakeCodeBuild()
    got = _build(client, ["web"])
    env = {v["name"]: v["value"] for v in client.started[0]["environmentVariablesOverride"]}
    assert env["IMAGE_REPO"] == IMAGE_REPO
    assert got.artifacts.images["web"].ref.startswith(IMAGE_REPO + "@")


def test_rejects_bad_image_repo_before_building() -> None:
    client = FakeCodeBuild()
    with pytest.raises(DdakToolError) as exc:
        _build(client, ["web"], image_repo="docker.io/gerbera/ddak:latest")
    assert exc.value.code is ErrorCode.CONFIG_INVALID
    assert client.started == []


def test_s3_fallback_passes_without_resolved_source_version() -> None:
    s3 = S3Source(bucket="ddak-source", key="flaskr/run.zip", version_id="v-123", revision=SHA)
    client = FakeCodeBuild()
    got = _build(client, ["web"], source=s3)
    assert "resolvedSourceVersion" not in client.batch_get_builds(ids=[got.build_id])["builds"][0]
    assert got.revision == SHA


def test_fake_codebuild_fails_without_buildspec_override() -> None:
    client = FakeCodeBuild()
    built = client.start_build(
        projectName="ddak-build",
        sourceVersion=SHA,
        environmentVariablesOverride=[
            {"name": "BUILD_TIERS", "value": "web"},
            {"name": "SOURCE_REVISION", "value": SHA},
        ],
    )
    status = client.batch_get_builds(ids=[built["build"]["id"]])["builds"][0]["buildStatus"]
    assert status == "FAILED"
