"""Docker Hub 어댑터(기본, ✅ 9/30). 담당 C2.

- CodeBuild: Docker Hub 읽기·쓰기 토큰(Secrets Manager)으로 로그인해 push.
- ECS: 태스크 정의 `repositoryCredentials.credentialsParameter` = 읽기 전용 토큰 시크릿 ARN.
  실행 역할은 그 시크릿만 읽는다. Fargate는 인터넷이 필요하다(💭 퍼블릭 서브넷 + 퍼블릭 IP).
- 온프렘: 읽기 전용 토큰으로 pull(AWS 자격 불필요).
- 확인 필요: 무료 플랜 비공개 저장소 수·pull 제한(저장소가 1개면 web·was를 태그로 나눔).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ddak.ci.registries._names import check_digest, check_name


@dataclass(frozen=True)
class DockerHub:
    name: ClassVar[str] = "dockerhub"
    namespace: str  # 조직 또는 사용자 네임스페이스(플랜에 따라 다름, 확인 필요)

    def image_ref(self, repository: str, digest: str) -> str:
        """`docker.io/<네임스페이스>/<저장소>@sha256:...` (digest 고정)."""
        ns = check_name(self.namespace, "네임스페이스")
        repo = check_name(repository, "저장소")
        return f"docker.io/{ns}/{repo}@{check_digest(digest)}"
