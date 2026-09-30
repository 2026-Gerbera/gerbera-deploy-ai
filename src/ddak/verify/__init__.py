"""verify: ⑤ 검증 및 보고. 담당 O3(장민영: smoke·compare·diagnose), C3(양서윤: report).

하위 디렉토리(각 __init__.py의 공개 함수만 다른 곳에서 쓴다):
- smoke/ smoke_test · compare/ compare_env_results(교차 검증)
- diagnose/ diagnose_parity_gap(AI 원인 분석) · report/ post_report(결과 카드·보고, AI 요약 선택)

- AI 호출은 diagnose·report에서만 ddak.core.ai로(계약 2).
- verify_tls는 cloud/health, health_check는 cd 툴(provider가 환경별 구현)이다.
- 디렉토리 미배정 툴(카탈로그 모듈 verify): watch_post_deploy, collect_diagnostics,
  record_deploy_log. 담당이 정해지면 디렉토리를 만든다.
- 툴 등록: 구현이 끝나면 그 디렉토리에 tool.py를 만들고 @tool("<이름>")으로 등록한다.
"""
