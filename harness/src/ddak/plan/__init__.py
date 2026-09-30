"""plan: ① 플랜 + ② 계획 검증. 담당 O2(김준석), patch_* 3개와 Dockerfile 생성·검사는 O3(장민영, 💭).

툴 10개: receive_deploy_request, analyze_project, detect_changed_tiers, generate_plan,
validate_plan, (Dockerfile이 없을 때만) generate_dockerfile, validate_dockerfile,
(토글 ON 전용) patch_db_access, patch_storage, patch_config.

- AI 툴(analyze_project, generate_plan, generate_dockerfile, patch_*)은 ddak.core.ai.gateway
  (call_ai, ask_jev)만 부른다. 제안(JSON, Dockerfile 초안)만 만들고 실행하지 않는다.
- validate_plan(조립기)·validate_dockerfile은 결정적 검사기다. AI와 생성기(generate_plan,
  generate_dockerfile)를 import하지 않는다(계약 5). 검사기와 생성기를 나눠야 "AI가 만든 것을 AI가
  통과시키는" 경로가 없다.
- AI 초안(PlanDraft) + step 카탈로그 + facts로 최종 plan.json을 만든다. 불합격이면 재지시 1회 ->
  규칙 계획(✅ 장부 5). step 카탈로그와 PlanDraft 모델은 이 모듈이 소유한다(TODO(O2)).
- Dockerfile(✅ 9/30): 없으면 AI 작성 -> 정적 검사(hadolint 등, 비root·버전 고정 베이스·비밀값 COPY
  금지) + 실제 빌드 성공 확인 -> 사람 승인 -> 저장·재사용. 흐름·위치는 💭(설계 문서 02).
- 인프라 제안·검증(AI Terraform)은 infra 모듈이다. 다른 툴 모듈을 import하지 않는다(계약 3).
"""
