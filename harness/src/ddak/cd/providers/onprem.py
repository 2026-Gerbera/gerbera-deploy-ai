"""온프렘 provider(target local). 담당 O1(정준우), health_check local은 O3(장민영).

- 설계는 tier별 서버(VM, DOCKER_HOST=ssh://), 자원이 부족하면 tier별 컨테이너(✅ 장부 10).
  주소·IP는 온프렘 인벤토리(김준석 작성) -> 환경 정보에서 읽는다.
- 이미지는 Docker Hub에서 읽기 전용 토큰으로 digest pull(✅ 9/30, AWS 자격 불필요).
- 진짜 방어선은 생성 필드 화이트리스트다: 이미지는 @sha256 digest로만, privileged·cap_add·
  bind mount·host network 거부, ddak.managed=true 라벨이 있는 컨테이너만 stop·rm.
  docker는 subprocess 리스트 인자로 부른다(shell=True 금지).
- ensure_tls는 해당 없음(로컬 HTTPS ⏸ 보류)을 돌려준다. 실패가 아니다.
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


class OnPremProvider:
    name: ClassVar[str] = ProviderName.ONPREM.value
    target: ClassVar[Target] = Target.LOCAL

    def deploy(self, tier: str, ctx: RunContext) -> ProviderResult:
        raise _todo("O1")

    def rollback(self, tier: str, ctx: RunContext) -> ProviderResult:
        raise _todo("O1")

    def health_check(self, ctx: RunContext) -> ProviderResult:
        raise _todo("O3")

    def migrate_db(self, migrations: Sequence[str], ctx: RunContext) -> ProviderResult:
        raise _todo("O1")

    def inject_config(self, keys: Sequence[str], ctx: RunContext) -> ProviderResult:
        raise _todo("O1")  # SECRET_KEY 난수는 was .env에(✅ 9/30 데모 시크릿 장면)

    def ensure_tls(self, mode: Literal["check", "apply"], ctx: RunContext) -> ProviderResult:
        del mode, ctx
        return ProviderResult(
            provider=self.name,
            function="ensure_tls",
            applicable=False,
            detail="해당 없음: 로컬 HTTPS는 보류(⏸)",
        )
