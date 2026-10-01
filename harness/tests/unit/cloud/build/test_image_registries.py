"""이미지 저장소 어댑터: 기본 Docker Hub(✅ 9/30), ECR 옵션. 배포 참조는 digest 고정만."""

from __future__ import annotations

import pytest

from ddak.cloud.build.registries import (
    DEFAULT_REGISTRY,
    REGISTRIES,
    DockerHub,
    Ecr,
    image_artifact,
    tag_for,
)
from ddak.core.contracts.errors import DdakToolError

DIGEST = "sha256:" + "a" * 64
INDEX = "sha256:" + "1" * 64
AMD64 = "sha256:" + "2" * 64
ARM64 = "sha256:" + "3" * 64
PLATFORMS = {"linux/amd64": AMD64, "linux/arm64": ARM64}


def test_default_registry_is_dockerhub_and_ecr_is_option() -> None:
    assert DEFAULT_REGISTRY == "dockerhub"
    assert set(REGISTRIES) == {"dockerhub", "ecr"}


def test_dockerhub_ref_is_digest_pinned() -> None:
    ref = DockerHub(namespace="ddak-team").image_ref("flaskr", DIGEST)
    assert ref == f"docker.io/ddak-team/flaskr@{DIGEST}"


@pytest.mark.parametrize("digest", ["latest", "v1", "sha256:abc", "sha256:" + "A" * 64])
def test_tags_or_bad_digests_are_rejected(digest: str) -> None:
    with pytest.raises(DdakToolError):
        DockerHub(namespace="ddak-team").image_ref("flaskr", digest)


def test_bad_names_are_rejected() -> None:
    with pytest.raises(DdakToolError):
        DockerHub(namespace="Ddak Team").image_ref("flaskr", DIGEST)
    with pytest.raises(DdakToolError):
        DockerHub(namespace="ddak-team").image_ref("flaskr:latest", DIGEST)


def test_ecr_option_ref() -> None:
    ref = Ecr(account_id="1" * 12).image_ref("ddak-was", DIGEST)
    assert ref == f"{'1' * 12}.dkr.ecr.ap-northeast-2.amazonaws.com/ddak-was@{DIGEST}"
    with pytest.raises(DdakToolError):
        Ecr(account_id="123").image_ref("ddak-was", DIGEST)


def test_single_repository_uses_tier_tags_for_display() -> None:
    # 무료 플랜 비공개 저장소가 1개면 web·was를 태그로 나눈다(확인 필요). 배포는 digest로만.
    assert tag_for("was", "rel-20261003-1412") == "was-rel-20261003-1412"


def test_image_artifact_pins_ref_to_index_digest() -> None:
    artifact = image_artifact(DockerHub(namespace="ddak-team"), "flaskr", INDEX, PLATFORMS)
    assert artifact.ref == f"docker.io/ddak-team/flaskr@{INDEX}"
    assert artifact.index_digest == INDEX
    assert artifact.platform_digests == PLATFORMS


def test_image_artifact_works_with_ecr_option() -> None:
    artifact = image_artifact(Ecr(account_id="1" * 12), "ddak-was", INDEX, PLATFORMS)
    assert artifact.ref.endswith(f"/ddak-was@{INDEX}")


@pytest.mark.parametrize("missing", ["linux/amd64", "linux/arm64"])
def test_image_artifact_requires_both_platforms(missing: str) -> None:
    platforms = {p: d for p, d in PLATFORMS.items() if p != missing}
    with pytest.raises(DdakToolError):
        image_artifact(DockerHub(namespace="ddak-team"), "flaskr", INDEX, platforms)


def test_image_artifact_rejects_unknown_platform() -> None:
    platforms = {**PLATFORMS, "linux/arm/v7": DIGEST}
    with pytest.raises(DdakToolError):
        image_artifact(DockerHub(namespace="ddak-team"), "flaskr", INDEX, platforms)


@pytest.mark.parametrize(
    ("index", "platforms"),
    [
        (AMD64, PLATFORMS),  # 플랫폼 digest로 참조를 만들면 한쪽 CPU에서만 뜬다
        (INDEX, {"linux/amd64": AMD64, "linux/arm64": AMD64}),
        ("latest", PLATFORMS),
        (INDEX, {"linux/amd64": "v1", "linux/arm64": ARM64}),
    ],
)
def test_image_artifact_rejects_mixed_or_bad_digests(index: str, platforms: dict[str, str]) -> None:
    with pytest.raises(DdakToolError):
        image_artifact(DockerHub(namespace="ddak-team"), "flaskr", index, platforms)


def test_push_ref_is_tagged_and_separate_from_deploy_ref() -> None:
    hub = DockerHub(namespace="ddak-team")
    tag = tag_for("was", "rel-20261003-1412-7f3c2a1")
    assert hub.push_ref("flaskr", tag) == f"docker.io/ddak-team/flaskr:{tag}"
    ecr = Ecr(account_id="1" * 12).push_ref("ddak-was", tag)
    assert ecr == f"{'1' * 12}.dkr.ecr.ap-northeast-2.amazonaws.com/ddak-was:{tag}"


@pytest.mark.parametrize("tag", ["latest", "", "-was", "was:v1", "was@sha256", "a" * 129, "was v1"])
def test_push_ref_rejects_bad_tags(tag: str) -> None:
    with pytest.raises(DdakToolError):
        DockerHub(namespace="ddak-team").push_ref("flaskr", tag)
