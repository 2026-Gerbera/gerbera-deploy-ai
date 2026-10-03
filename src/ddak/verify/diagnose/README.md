# verify/diagnose (담당: 장민영)
- 할 일: 원인 분석(diagnose_parity_gap). **설명 전용**(2026-10-03 diagnose-advisory 결정: 실행기가 compare 불합격·트랙 실패 뒤에만 부르고 판정·롤백에 쓰지 않음)
- 툴: `advisory.py`의 `diagnose_parity_gap` + `tool.py` 등록. 입력은 실행기가 채우는 `reason`·`tracks`·`failed_steps`(redact된 실패 메시지, 툴 출력), smoke 전체는 `verify.smoke` 보관소. 출력에 passed 없음. AI 설명(규칙이 못 잡을 때)은 아직 없고 `source`·`ai_usage` 자리만 있다
- 규칙 부분(AI 없음): `rules.py`의 `diagnose_by_rules(logs, smoke, compare)`. 알려진 실패 문구(MissingEnvError, ResourceInitializationError, MySQL 1146·1045·3159·2003, CERTIFICATE_VERIFY_FAILED, CannotPullContainerError, ALB unhealthy 등) → 범주. 근거는 위치만(log#줄, smoke:시나리오, diff:검사 id), 원문은 싣지 않는다.
- 입출력 계약: `src/ddak/core/contracts/tools/diagnose_parity_gap.py`(초안, 신규)
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(여기는 허용: import-linter 계약 2).
