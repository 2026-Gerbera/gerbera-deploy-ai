# verify/report (담당: 양서윤)
- 구현: 규칙 기반 결과 카드·보고(post_report). AI 요약은 선택(P1)
- 입출력 계약: `src/ddak/core/contracts` (이 툴의 <Tool>Input/Output 모델은 아직 없다. 입출력은 __init__.py docstring)
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(여기는 허용: import-linter 계약 2).
