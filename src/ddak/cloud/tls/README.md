# cloud/tls (담당: 유상준)
- 할 일: HTTPS 연결(ensure_tls). 클라우드만
- 입출력 계약: `src/ddak/core/contracts`, CD 인터페이스는 `src/ddak/cd/interface.py`(ProviderResult)
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(여기는 금지: import-linter 계약이 막는다).
