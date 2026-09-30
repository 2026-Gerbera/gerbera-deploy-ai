"""AI 관문(call_ai, ask_jev). 담당 O2(김준석).

- AI는 제안만 만든다(✅ 장부 4-a): JSON(채팅 의도, 분석 분류, step 선택, 실패 원인 설명,
  보고 요약)과 Terraform HCL 초안(generate_infra).
  제안은 검증과 사람 승인을 거친 뒤 코드가 실행한다.
- 실행(빌드·배포·검증·롤백·잠금·인프라 apply)은 항상 코드다. LLM은 툴을 직접 호출하지 않는다.
- LLM·Jev SDK import는 ddak.core.ai.providers에서만 허용한다(ruff TID251 예외).
- 이 패키지는 허용된 AI 툴 모듈과 조립 진입점 ddak.app만 import할 수 있다(import-linter 계약 2).
"""
