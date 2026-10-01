"""C1 결정적 내부 API. generate_infra는 O2 소유이며 여기서 import하지 않는다.

공유 입출력 모델·레지스트리·RunContext 연결은 계약 합의 뒤 별도로 조립한다.
plan과 apply는 신뢰하는 승인 조회·잠금 검사·단기 세션을 주입받아 실행한다.
"""

from .foundation import apply_foundation, foundation_template
from .policy import GateResult, static_gate
from .runtime import AwsSettings, InfraRuntime, SessionKeys

__all__ = [
    "AwsSettings",
    "GateResult",
    "InfraRuntime",
    "SessionKeys",
    "apply_foundation",
    "foundation_template",
    "static_gate",
]
