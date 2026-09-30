# plan/detect (담당: 김준석)
- 할 일: 변경 탐지·스냅샷 해시(detect_changed_tiers)
- 입출력 계약: `src/ddak/core/contracts` (이 툴의 <Tool>Input/Output 모델은 아직 없다. 입출력은 __init__.py docstring)
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(여기는 금지: import-linter 계약이 막는다).
