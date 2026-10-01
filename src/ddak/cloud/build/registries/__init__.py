"""이미지 저장소 어댑터(💭). 기본 = Docker Hub(✅ 9/30), ECR은 옵션.

툴 레지스트리(core.registry)와 다른 것이다. 여기는 컨테이너 이미지 저장소(registry)다.
- 배포 참조는 항상 digest 고정(`<저장소>@sha256:<64hex>`). 태그로 배포하지 않는다.
- 저장소 선택은 deploy.yaml `targets.cloud.registry`(코드)로 한다. AI가 고르지 않는다.
- 푸시 토큰 값은 여기서 다루지 않는다. CodeBuild가 Secrets Manager에서 읽는다(이름만 설정).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar, Protocol

from pydantic import ValidationError

from ddak.cloud.build.registries._names import check_digest
from ddak.cloud.build.registries.dockerhub import DockerHub
from ddak.cloud.build.registries.ecr import Ecr
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.release import ImageArtifact

PLATFORMS = ("linux/amd64", "linux/arm64")

DEFAULT_REGISTRY = "dockerhub"
REGISTRIES: Mapping[str, type] = MappingProxyType({DockerHub.name: DockerHub, Ecr.name: Ecr})


class ImageRegistry(Protocol):
    name: ClassVar[str]

    def image_ref(self, repository: str, digest: str) -> str: ...

    def push_ref(self, repository: str, tag: str) -> str: ...


def tag_for(tier: str, release_id: str) -> str:
    """저장소 1개에 web·was를 태그로 나눌 때의 표시용 태그(💭). 배포는 digest로만 한다."""
    return f"{tier}-{release_id}"


def image_artifact(
    registry: ImageRegistry,
    repository: str,
    index_digest: str,
    platform_digests: Mapping[str, str],
) -> ImageArtifact:
    """멀티 아키텍처 빌드 결과를 릴리스 계약(ImageArtifact)으로 묶는다.

    배포 참조는 index digest에 고정하고, 플랫폼 digest(amd64·arm64)는 환경별 관측 대조용으로 남긴다.
    플랫폼 digest로 참조를 만드는 실수(한쪽 CPU에서만 뜨는 이미지)를 막는다.
    """
    if set(platform_digests) != set(PLATFORMS):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "amd64와 arm64 플랫폼 digest가 모두 필요하다")
    index = check_digest(index_digest)
    platforms = {p: check_digest(platform_digests[p]) for p in PLATFORMS}
    if index in platforms.values():
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "index digest와 플랫폼 digest가 같다")
    if len(set(platforms.values())) != len(PLATFORMS):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "플랫폼 digest가 서로 같다")
    try:
        return ImageArtifact(
            ref=registry.image_ref(repository, index),
            index_digest=index,
            platform_digests=platforms,  # pyright: ignore[reportArgumentType]
        )
    except ValidationError as exc:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "이미지 산출물 형식이 아니다") from exc


__all__ = [
    "DEFAULT_REGISTRY",
    "PLATFORMS",
    "REGISTRIES",
    "DockerHub",
    "Ecr",
    "ImageRegistry",
    "image_artifact",
    "tag_for",
]
