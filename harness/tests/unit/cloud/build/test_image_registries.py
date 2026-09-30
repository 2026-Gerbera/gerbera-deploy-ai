"""이미지 저장소 어댑터: 기본 Docker Hub(✅ 9/30), ECR 옵션. 배포 참조는 digest 고정만."""

from __future__ import annotations

import pytest

from ddak.cloud.build.registries import DEFAULT_REGISTRY, REGISTRIES, DockerHub, Ecr, tag_for
from ddak.core.contracts.errors import DdakToolError

DIGEST = "sha256:" + "a" * 64


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
