# cloud/health (담당: 양서윤)
- 구현: 클라우드 헬스(health_check cloud)·TLS 검증(verify_tls)·결정적 fake
- CSP 분리: `providers/aws.py`, `providers/gcp.py`, `providers/azure.py`. AWS는 실제 구현에
  연결되고 GCP·Azure는 잘못된 교차 배포를 막는 명시적 미지원 구현이다.
- 실제 모드는 읽기 전용 AWS 프로필(`DDAK_AWS_READONLY_PROFILE`, 기본 `ddak-readonly`)만 쓴다.
- 입출력 계약: `src/ddak/core/contracts`, CD 인터페이스는 `src/ddak/cd/interface.py`(ProviderResult)
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(여기는 금지: import-linter 계약이 막는다).
