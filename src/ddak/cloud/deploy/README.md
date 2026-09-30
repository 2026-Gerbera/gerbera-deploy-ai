# cloud/deploy (담당: 안승환)
- 할 일: ECS 배포·롤백(ecs.py), 시크릿 값(secrets.py), DB 마이그레이션(database.py). `__init__.py`가 AwsProvider(CD 인터페이스 구현)를 노출
- 입출력 계약: `src/ddak/core/contracts`, CD 인터페이스는 `src/ddak/cd/interface.py`(ProviderResult)
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(여기는 금지: import-linter 계약이 막는다).
