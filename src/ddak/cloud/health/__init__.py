"""cloud/health: 클라우드 헬스·TLS 검증. 담당 양서윤(C3).

공개 함수: health_check(CD 인터페이스 health_check의 AWS 구현, AwsProvider가 부른다),
verify_tls(카탈로그 모듈 cloud.health, 구현이 끝나면 tool.py에서 @tool("verify_tls")로 등록).
AI import 금지(import-linter 계약 1). 빈 구현이다.
"""

from __future__ import annotations

from ddak.cd.interface import ProviderResult
from ddak.core.contracts.context import RunContext

_TODO = "cloud/health 미구현: 담당 양서윤"


def health_check(ctx: RunContext) -> ProviderResult:
    """ALB/서비스 헬스 확인. 출력: ProviderResult(function="health_check")."""
    raise NotImplementedError(_TODO)


def verify_tls(inp: object, ctx: RunContext) -> object:
    """verify_tls 빈 구현. VerifyTlsInput/Output 모델은 아직 없다.

    입력: 대상 도메인(RunContext.cloud_domain). 출력: 인증서 유효성·체인·HTTP->HTTPS 결과.
    """
    raise NotImplementedError(_TODO)
