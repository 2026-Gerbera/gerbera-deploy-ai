"""ECR 어댑터(옵션, 💭). 기본은 Docker Hub다(✅ 9/30).

ECR을 켜면 CodeBuild·실행 역할·권한 경계에 ECR 권한을 다시 넣어야 한다
(research/IAM 14절의 "ECR 옵션" 표). 온프렘은 AWS 자격(ecr:GetAuthorizationToken)이 다시 필요해진다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar

from ddak.cloud.build.registries._names import check_digest, check_name, check_tag
from ddak.core.contracts.errors import DdakToolError, ErrorCode

_ACCOUNT = re.compile(r"^[0-9]{12}$")


@dataclass(frozen=True)
class Ecr:
    name: ClassVar[str] = "ecr"
    account_id: str
    region: str = "ap-northeast-2"

    def _host(self) -> str:
        if not _ACCOUNT.fullmatch(self.account_id):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "계정 ID 형식이 아니다")
        return f"{self.account_id}.dkr.ecr.{self.region}.amazonaws.com"

    def image_ref(self, repository: str, digest: str) -> str:
        repo = check_name(repository, "저장소")
        return f"{self._host()}/{repo}@{check_digest(digest)}"

    def push_ref(self, repository: str, tag: str) -> str:
        """push 대상(태그). 배포에는 쓰지 않는다."""
        repo = check_name(repository, "저장소")
        return f"{self._host()}/{repo}:{check_tag(tag)}"
