# onprem/provision (담당: 김준석)
- `prepare_host(ctx) -> HostCheck`: 점검만(변경 없음). FAKE는 결정적 결과, REAL은 `docker info`와 인벤토리 네트워크 존재를 `docker network inspect`로 확인(리스트 인자, shell=False, 타임아웃 15초).
- `ensure_app_database`: 미구현(결정 대기: DB 접근 방식, 비밀번호 저장 위치, O1 `prepare_db` 경계). 호출하면 `NotImplementedError`.
- AI 호출 금지(import-linter 계약).
