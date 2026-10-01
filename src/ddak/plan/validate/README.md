# plan/validate (담당: 김준석)
- `validate_plan(ValidatePlanInput, RunContext) -> Plan`: 조립기이자 검사기. 순수 함수, AI·생성기 import 금지(계약 5).
- 규칙은 `rules.py`(R-ids, R-params, R-mandatory, R-couple, R-gate, R-facts + 카탈로그 skip_rule), 조립은 `assemble.py`, 등록은 `tool.py`.
- `draft=None` = 규칙 계획(폴백). 필수 step을 뺀 AI 결정은 `Plan.invalidated`에 기록하고 강제 포함한다.
- 토글 `ctx.toggles["validate_ai_draft"]`(기본 True). 끄면 미지 id·허용 밖 파라미터를 거부 대신 버리고 경고,
  조건부 불일치 기록을 생략한다. 금지 파라미터 거부·필수 포함·카탈로그 조립·R-couple·R-gate·R-facts는 끌 수 없다.
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 `__init__.py`의 공개 함수만 쓴다.
