# fixtures — 결정적 입력

> 기준: 앱 1개 구조(✅ 9/30 최신 개발자 문서 기준)

- 데모와 테스트가 매번 같은 결과를 내도록 **결정적인 입력만** 둡니다.
- 있음: `plans/golden_v2_update.json`(골든 패스 v2 plan.json, 설계 문서 02 6-4와 같은 내용, `tests/contract/test_plan_example.py`가 확인). 9/30 데모 시크릿 장면 결정으로 클라우드 트랙에 인프라 step(`deploy.infra.cloud`, G1 뒤)이 들어 있습니다.
- 예정 항목(💭): `deploy.yaml` 예시(도메인 없음), 온프렘 인벤토리 예시, 부트스트랩 plan.json, `ai_replay/`(call_ai replay backend 저장 응답), `patch_cases/`(O3 소유, 설정 패턴은 P0·데모 ON), `demo/`.
- 데이터 형식은 `docs/contracts/` 계약 문서가 정합니다. 여기서 새 필드를 만들지 않습니다.
- AI 저장 응답은 결과에 `source` 라벨(`replay`/`cache`/`fixture`)이 드러나게 합니다. redact를 거친 입력으로만 키를 만듭니다.
- 번호 붙은 공유 파일을 여러 명이 동시에 추가하지 않습니다.
