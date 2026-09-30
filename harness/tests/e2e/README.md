# tests/e2e — Fake 모드 전체 경로

> 기준: 앱 1개 구조(✅ 9/30 최신 개발자 문서 기준)

- 담당: 정준우(O1)
- 앱 1개 구조라 MCP Client 배선이 없습니다. `ddak.app.load_tools()`로 레지스트리를 채우고, 모든 어댑터를 Fake로 둔 채 골든 plan.json 하나를 실행기로 끝까지 실행합니다.
- 최소 예시는 `tests/unit/test_executor.py`(테스트용 레지스트리)와 `tests/unit/test_ping.py`(실제 레지스트리 + ping)입니다.
- 통합 창구 뒤에 O1이 main 기준으로 `make run-fake`와 `make contract-smoke`를 함께 돌립니다.
- 실제 Docker·AWS·LLM을 쓰는 테스트는 `docker`/`aws`/`llm` 마커를 붙여 CI에서 제외합니다.
