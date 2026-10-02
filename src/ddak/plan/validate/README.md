# plan/validate (담당: 김준석)
- `validate_plan(ValidatePlanInput, RunContext) -> Plan`: 조립기이자 검사기. 순수 함수, AI·생성기 import 금지(계약 5).
- 규칙은 `rules.py`(R-ids, R-params, R-mandatory, R-couple, R-gate, R-facts + 카탈로그 skip_rule), 조립은 `assemble.py`, 등록은 `tool.py`.
- `draft=None` = 규칙 계획(폴백). 필수 step을 뺀 AI 결정은 `Plan.invalidated`에 기록하고 강제 포함한다.
- 검사 모드 `ctx.toggles["strict_ai_check"]`(없으면 False = 기본 검사). 기본: 미지 id·허용 밖 파라미터를 거부 대신 버리고 경고(재지시 없음).
  엄격: 거부(PLAN_INVALID). 두 모드 공통(끌 수 없음): 금지 파라미터 거부·필수 포함·카탈로그 조립·R-couple·R-gate·R-facts·R-migration.
- `Plan.invalidated`: AI 뺌 -> 강제 포함, AI 포함 요청 -> 규칙 제외(`attempt="include", result="forced_skip"`)도 기록(두 모드 공통).
- R-migration: 새 마이그레이션이 있으면 `deploy.migrate.<env>`가 정확한 params로 tier 배포보다 앞에 있어야 하고, 없으면 없어야 한다. `modified_migrations`는 경고 `migration_modified`(엄격은 거부).
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 `__init__.py`의 공개 함수만 쓴다.
