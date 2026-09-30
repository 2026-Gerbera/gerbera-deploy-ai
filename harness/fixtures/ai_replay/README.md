# fixtures/ai_replay — call_ai replay backend 저장 응답

> 기준: 앱 1개 구조(✅ 9/30 최신 개발자 문서 기준)

- 위치: `fixtures/ai_replay/<툴 이름>/<키>.json`. 키는 purpose·prompt 버전·시스템 프롬프트·redact된 입력·출력 스키마의 sha256입니다(`ddak.core.ai.providers.replay.replay_key`).
- 파일에는 출력과 메타정보만 둡니다. 입력 원문은 저장하지 않습니다.
- 결과에는 `source=replay` 라벨이 붙고 관리 페이지가 "저장된 응답"으로 표시합니다(목업은 목업이라고 밝힘).
- 리허설에서 live 응답을 녹화할 때는 사람이 `ReplayProvider.save()`를 부릅니다. 비밀값이 섞이지 않았는지 커밋 전에 확인합니다.
