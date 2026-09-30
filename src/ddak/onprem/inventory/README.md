# onprem/inventory (담당: 김준석)
- 할 일: 인벤토리(tier별 주소) 읽기
- 입출력 계약: `src/ddak/core/contracts` (모양은 `src/ddak/onprem/deploy/provider.py` docstring의 인벤토리 계약)
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(여기는 금지: import-linter 계약이 막는다).
