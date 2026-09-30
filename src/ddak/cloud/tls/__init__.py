"""cloud/tls: HTTPS 연결(ACM·ALB 리스너·도메인). 담당 유상준(C1).

공개 함수: ensure_tls. CD 인터페이스 ensure_tls의 AWS 구현이다(AwsProvider가 부른다).
도메인은 사람이 관리 페이지에 입력한 값(RunContext.cloud_domain)만 쓴다. AI import 금지.
빈 구현이다.
"""

from __future__ import annotations

from typing import Literal

from ddak.cd.interface import ProviderResult
from ddak.core.contracts.context import RunContext

_TODO = "cloud/tls 미구현: 담당 유상준"


def ensure_tls(mode: Literal["check", "apply"], ctx: RunContext) -> ProviderResult:
    """check: 인증서·리스너 상태 확인만. apply: "도메인 연결" run에서만 발급·연결.

    출력: ProviderResult(function="ensure_tls").
    """
    raise NotImplementedError(_TODO)
