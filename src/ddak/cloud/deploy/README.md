# cloud/deploy (담당: 안승환)
- 할 일: CSP별 provider는 `providers/aws.py`, `providers/gcp.py`, `providers/azure.py`로
  분리한다. AWS는 ECS 배포·롤백(ecs.py), 시크릿 값(secrets.py), DB 마이그레이션(database.py)에
  연결하고, GCP·Azure는 실제 구현 전까지 명시적인 미지원 오류를 반환한다.
- 입출력 계약: `src/ddak/core/contracts`, CD 인터페이스는 `src/ddak/cd/interface.py`(ProviderResult)
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(여기는 금지: import-linter 계약이 막는다).
