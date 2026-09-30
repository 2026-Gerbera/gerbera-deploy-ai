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

from ddak.ci.registries.dockerhub import DockerHub
from ddak.ci.registries.ecr import Ecr

DEFAULT_REGISTRY = "dockerhub"
REGISTRIES: Mapping[str, type] = MappingProxyType({DockerHub.name: DockerHub, Ecr.name: Ecr})


class ImageRegistry(Protocol):
    name: ClassVar[str]

    def image_ref(self, repository: str, digest: str) -> str: ...


def tag_for(tier: str, release_id: str) -> str:
    """저장소 1개에 web·was를 태그로 나눌 때의 표시용 태그(💭). 배포는 digest로만 한다."""
    return f"{tier}-{release_id}"


__all__ = ["DEFAULT_REGISTRY", "REGISTRIES", "DockerHub", "Ecr", "ImageRegistry", "tag_for"]
