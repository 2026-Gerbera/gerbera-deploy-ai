# verify/diagnose (담당: 장민영)
- 할 일: 원인 분석(diagnose_parity_gap, AI 설명)
- 규칙 부분(AI 없음): `rules.py`의 `diagnose_by_rules(logs, smoke, compare)`. 알려진 실패 문구(MissingEnvError, ResourceInitializationError, MySQL 1146·1045·3159·2003, CERTIFICATE_VERIFY_FAILED, CannotPullContainerError, ALB unhealthy 등) → 범주. 근거는 위치만(log#줄, smoke:시나리오, diff:검사 id), 원문은 싣지 않는다. 아직 툴로 등록하지 않는다(입출력 계약 모델은 정준우 확인 뒤)
- 입출력 계약: `src/ddak/core/contracts` (이 툴의 <Tool>Input/Output 모델은 아직 없다. 입출력은 __init__.py docstring)
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(여기는 허용: import-linter 계약 2).
