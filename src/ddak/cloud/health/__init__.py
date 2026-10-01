"""클라우드 헬스와 TLS 검증의 공개 API(C3).

현재 실행 기본값은 AWS다. GCP·Azure 구현은 providers 패키지에 분리되어 있으며 지원 전에는
명시적 오류를 반환한다.
"""

from ddak.cloud.health.providers.aws import health_check, verify_tls

__all__ = ["health_check", "verify_tls"]
