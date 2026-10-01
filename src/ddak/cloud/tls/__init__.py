"""C1 TLS 상태 확인. 발급/리스너 변경 소유권 확정 전 apply는 차단한다."""

from .check import check_tls, ensure_tls

__all__ = ["check_tls", "ensure_tls"]
