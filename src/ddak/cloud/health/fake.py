"""AWS 없이 UI와 실행기 연결을 확인하는 결정적 C3 어댑터."""

from ddak.core.contracts.enums import Target
from ddak.core.contracts.tools.verify_tls import TlsCheck, VerifyTlsOutput


def fake_verify_tls(run_id: str) -> VerifyTlsOutput:
    names = {
        "V1": "DNS 연결",
        "V2": "인증서 체인·호스트명",
        "V3": "TLS 버전",
        "V4": "인증서 내용",
        "V5": "ACM 연결",
        "V6": "ALB 리스너",
        "V7": "HTTP 리다이렉트",
        "V8": "구형 TLS 거부",
        "V9": "HSTS",
    }
    return VerifyTlsOutput(
        run_id=run_id,
        target=Target.CLOUD,
        passed=True,
        checks=[
            TlsCheck(id=key, name=name, status="pass", detail="fake 검증 통과")
            for key, name in names.items()
        ],
        elapsed_s=0,
    )
