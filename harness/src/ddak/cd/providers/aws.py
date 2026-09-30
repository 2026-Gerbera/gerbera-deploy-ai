"""AWS provider(target cloud). 담당 C2(안승환), ensure_tls는 C1(유상준), health_check는 C3.

- ECS Fargate: 태스크 정의 새 리비전(이미지 = 환경 정보의 digest 고정 참조, `secrets` valueFrom =
  인프라 출력의 시크릿 ARN, Docker Hub면 `repositoryCredentials` = 읽기 전용 토큰 시크릿 ARN) ->
  update_service. 서비스는 Terraform이 desired 0 + `ignore_changes`로 만들어 두고
  배포는 여기서 한다.
- 시크릿 값: Terraform이 만든 빈 시크릿에 PutSecretValue(값은 코드 난수, state·AI·화면에 없음).
- boto3 클라이언트는 sync라 함수를 동기로 둔다. 툴 호출마다 새 Session. 읽기 전용 검증 프로필은
  DDAK_AWS_READONLY_PROFILE(값이 없으면 ddak-readonly로 고정, 쓰기 프로필로 떨어지지 않게).
- 파일이 커지면 함수별 모듈로 나눈다(💭, CODEOWNERS를 담당별로).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar, Literal

from ddak.cd.interface import ProviderName, ProviderResult
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode


def _todo(owner: str) -> DdakToolError:
    return DdakToolError(ErrorCode.INTERNAL, f"미구현: TODO({owner})")


class AwsProvider:
    name: ClassVar[str] = ProviderName.AWS.value
    target: ClassVar[Target] = Target.CLOUD

    def deploy(self, tier: str, ctx: RunContext) -> ProviderResult:
        raise _todo("C2")

    def rollback(self, tier: str, ctx: RunContext) -> ProviderResult:
        raise _todo("C2")

    def health_check(self, ctx: RunContext) -> ProviderResult:
        raise _todo("C3")

    def migrate_db(self, migrations: Sequence[str], ctx: RunContext) -> ProviderResult:
        raise _todo("C2")

    def inject_config(self, keys: Sequence[str], ctx: RunContext) -> ProviderResult:
        raise _todo("C2")

    def ensure_tls(self, mode: Literal["check", "apply"], ctx: RunContext) -> ProviderResult:
        raise _todo("C1")
