# 2026-10-03 수정10 관리자 UI 통합

정준우 지시로 origin/o1/exec-fix8(83e0b69)에 origin/cloud(486b4b1)를 별도 worktree에서 no-commit merge했다. C3 파일 수정은 이번 UI 통합에 한해 승인됐다. 서윤 확인 필요 목록은 [인계서](../guides/O1-ui-integration-handoff-2026-10-03.md)에 있다.

- 관리 UI는 `/runs/{id}/approval` 템플릿으로 통일하고 `/ops`의 이전 승인 URL은 호환 핸들러로 남긴다. 승인 대상/해시/실행 검증을 별도로 만들지 않는다.
- 프로젝트는 명시 선택 → DDAK_WATCH_PROJECT → flaskr 순서이며 기존 demo 별칭을 보존한다. 기본 대상은 ProjectSettings의 onprem, onprem의 cloud_domain은 선택이다.
- UI의 성공 봉인 표시는 SUCCEEDED에 한정한다. 실패 기록 자체의 봉인은 계속 유지하며 성공으로 표시하지 않는다.
- 공개 링크는 실행의 인벤토리 public_url 및 cloud_domain 스냅샷이다. 링크 표시가 현재 배포 성공/도달 가능성 판정은 아니다.
- `PLATFORM_OUTPUTS` 중복 이름 3개를 제거하고 사용자 지시대로 task_execution_role_arn/task_role_arn/dbinit_execution_role_arn/app_secret_arn_SECRET_KEY 4개를 보존한다. **NEEDS_CONTEXT:** APP 층과의 소유권/후속 갱신 경계 합의 필요. 이번에 새 JSON 스키마 필드나 상태 값은 추가하지 않았고 스키마 검사는 일치했다.
- FastAPI TestClient 검증을 위해 개발 의존성 httpx2를 추가한다. 제품 runtime 의존성과 기존 작업 트리의 환경은 변경하지 않는다.
- 실제 AWS/VM/Docker/Claude 실행 및 commit/push/PR은 하지 않는다. cloud 생성기의 프롬프트/정책, 호출 제한, DNS 결정과 Host 허용 범위는 인계한다.


## 추가1 결정 — 자동 감시 소유 프로젝트 중복 방지

정준우의 실측 중복 run 보고를 반영한다. 같은 저장소·브랜치에 auto_detect를 켠 다른 프로젝트가 있으면 SQLite 저장 트랜잭션에서 거부한다. 이미 저장된 중복은 감시 조립에서 DDAK_WATCH_PROJECT 우선으로 하나만 선택하고 제외 목록을 대시보드·운영·설정 화면과 로그에 표시한다. 저장 설정이 없을 때만 환경변수 후보를 보충하며, 사람의 비활성 설정을 환경변수로 다시 켜지 않는다.

기존 중복 설정·AWAITING_APPROVAL 기록은 자동 수정하지 않는다. 운영자가 제외 프로젝트의 auto_detect를 끄고 기존 중복 run은 거절한다. 자동 감시 중복 차단은 수동 배포를 새로 금지하는 규칙이 아니다. watch.py 및 공유 스키마 필드는 변경하지 않고 app 조립·기존 설정 저장 경계에서 처리한다. GitHub URL의 일반 표기 차이와 refs/heads 접두어만 정규화하며 mirror·모든 URL 별칭까지 추정하지 않는다.
