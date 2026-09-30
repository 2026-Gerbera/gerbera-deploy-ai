"""verify: ⑤ 검증 및 보고.

담당 O3(장민영: 교차 검증·원인 설명), C3(양서윤: 클라우드 검증·보고).

툴 8개: smoke_test, verify_tls, compare_env_results, watch_post_deploy, collect_diagnostics,
diagnose_parity_gap, record_deploy_log, post_report. (health_check는 CD 인터페이스 함수라 cd 모듈)
- AI 툴은 diagnose_parity_gap(원인 설명)과 post_report(보고 요약, 선택) 두 개뿐이다.
  나머지 툴 모듈은 ddak.core.ai를 import하지 않는다(계약 2가 직접 import를 막는다).
- verify_tls는 cloud만(✅ 장부 17).
- 💭 Slack 알림(P0-라이트, Incoming Webhook)은 보고 결과를 단방향으로 보내는 창구다. 담당 미정
  (양서윤 C3가 자연스러움). 웹훅 URL은 비밀로 다룬다.
"""
