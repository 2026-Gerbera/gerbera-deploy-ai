# 후속 수정 3·4 인계 — 2026-10-02

기준 afc3031. 로컬 파일이 최신이며 커밋·push·PR은 하지 않았다. 실제 AWS/GitHub push/WSL/VM 및 프로세스 종료 명령 없음.
개발 에이전트가 Terraform을 직접 실행하는 것과 **승인 뒤 제품 코드가 실행하는 것**을 구분한다.
상세 결정은 [후속3·4 기록](../decisions/2026-10-02-followup3-4.md), 명령·시간은 [측정 기록](../benchmarks/2026-10-02-followup3-4/README.md).

## 구현 결과

| 항목 | 결과 | 실제 연결 한계 |
|---|---|---|
| 3-0 승인 기반 첫 인프라 | 기반 버킷·권한 경계와 플랫폼 plan/backend를 한 infra 해시에 결합. 승인 뒤 SDK 기반 확보→플랫폼 apply→잠금 아래 S3 state 이전 | SDK/CLI fixture. O2 생성기 번들·세션 binding 및 외부 DNS 연결 미완, 실 AWS 배포 미검증 |
| 3-0-2 앱 저장소 | 설정 URL로 checkout factory 조립, 승인 URL과 fetch/push URL 대조, 설정 기반 감시 및 재시작 연결 | 로컬 bare 검증. 실행 호스트 Git 신원/인증·권한 필요 |
| 3-1·2 후보 호환 | 이전 ai-prod deploy.yaml CONFIG_INVALID만 prod 설정으로 대체. 일반 경로 대소문자 유지, 비밀 이름만 대소문자 무시 차단 | 후보 비밀값 검사 유지 |
| 3-3 문서 | 승인 트리 merge·실행 사용자 author·dev→prod/ai-prod/main 이전·O2 감시 요청·데모 2단계 반영 | 패치 OFF 이전 패치 제거는 정준우 확인 대기 표시 유지 |
| 4-1 annotated 태그 | 완전한 태그 ref의 peeled commit SHA 우선. annotated v1→v2 이미지/박스→v1 왕복 | O2 fetch.py 자체는 미수정 |
| 4-2 저장소 연결 | 3-0-2의 factory를 실제 app.create/prepare/start 경로에 주입 | 실제 GitHub push는 미실행 |
| 4-3 준비 실패 진단 | plan.json·설정 버전 보존, CONFIG_INVALID+missing_tool, 실패 run 승인 요청 PRECONDITION_FAILED | 웹 표시/HTTP 대응은 C3 |
| 4 추가: DB 멱등 | precheck current==expected이면 up 생략·verify 필수. dbinit→0001 연속 호출에서 up 한 번 | Fake Docker+SQLite, 실제 앱/MySQL 재리허설 필요 |
| 4 추가: 소스 정리 | 실패한 요청이 새로 만든 sources/run_id만 삭제. 기존 경로와 승인·실행 기록 보존 | 삭제 OS 오류 시 경고 기록, 운영자 정리 필요 |
| 4-4 태그 의미 | deployed/*는 실제 배포된 상태. 비교 불가 FAILED_VERIFY에서 태그 이동/main 보류 | 결정 기록만, 실행 정책 변경 없음 |

## 최종 검증

`make -C harness ci`: **1164 passed, 1 skipped, 4 deselected**, lint/type/boundary/contracts PASS. wrapper 82.184초, pytest 80.08초. skip 1건은 별도 temp-box uv 프로젝트, 외부 Docker/AWS/LLM 4건은 기본 CI 제외다. 후속3 코드 CI는 1146개 통과했다.

## 담당자 요청

| 담당 | 요청 |
|---|---|
| 준석(O2) | `fetch.py` annotated 태그 peel 순서 수정. TLS 인증서 오류를 transient로 3회 재시도하지 않도록 분류. ref 조회 실패에 요청 ref와 존재하는 브랜치 목록 표시 |
| 준석(O2) | `watch.py` standalone 기본 main→prod 정정. app은 이미 저장된 watch_branch/auto_detect를 공급. `db_initialized`를 컨트롤러 성공 이력만으로 판단하지 말고 실제 DB/버전과 조합; 새 컨트롤러의 기존 DB를 신규로 오판하는 문제 |
| 준석(O2/C1) | generate_infra 번들·C-20 출력·세션·영속 InfraBinding 조립. 기존 main을 덮는 임의 가짜 생성기 등록은 하지 않음. 새 앱 DB/계정은 기존 temp-box DB를 삭제하지 않고 별도 이름으로 생성하는 안 NEEDS_CONTEXT 유지 |
| 승환(C2) | `build_image` 등록이 실환경 첫 막힘. CodeBuild sourceVersion=candidate_sha, 반환 commit SHA/이미지 라벨/ReleaseArtifacts. cloud sync_env_to_cloud·push_image와 deploy 결과 연결 |
| 민영(O3) | `smoke_test`, `compare_env_results` tool 등록. 실제 앱 migrate precheck 읽기 검사/멱등 up/verify 규약 확인. 패치 재사용이 새 prod에 안 맞으면 패치 단계에서 재제안 |
| 서윤(C3) — 우선 | 설정 저장은 공개 `save_project_settings(..., expected_version=...)`. 시작 버튼은 `await service.request_deployment(project, targets=None, ref=None) -> run_id`; ref는 감시 브랜치 또는 v* 태그만. 반환 뒤 get_run 상태 확인 |
| 서윤(C3) | 설정에 repo_url·watch_branch·default_targets·auto_detect 추가. 온프렘 전용은 cloud_domain 없이 저장 허용. 온프렘 단독 실패에 ‘클라우드 변경 전 중단’ 잘못 표시 수정 |
| 서윤(C3) | `get_run(run_id)`의 result.phase/code/detail/missing_tool 표시. 실패 run approval_view의 DdakToolError를 승인 불가 4xx로 처리. SSE /events 501 정리 |

## NEEDS_CONTEXT — 혼합 초기 상태

단일 대상은 해당 환경 기록으로 모드를 정한다. both에서 한 환경만 기존이면 전역 RunMode를 임의로 적용하지 않고 미배포 대상만 선택하도록 안내한다. 환경별 서로 다른 모드를 한 run에 지원하려면 O2와 공유 계약 합의가 필요하다.

## 실환경 남은 막힘

현재 E2E 시나리오에 필요한 미등록 툴은 **build_image, smoke_test, compare_env_results**다.
전체 카탈로그 미등록 목록은 roundtrip.json에 별도 저장하며, 내부 Store/운영 스크립트 기능까지 ‘미구현’으로 섞지 않는다.
1차 로컬 Git 왕복 통과는 실제 빌드·VM·AWS·LLM 완성이나 3분 보장을 뜻하지 않는다.

## 커밋 제안

이번 변경은 같은 파일의 hunk가 후속3·4에 걸쳐 있으므로 단계별 강제 분할보다 다음 두 묶음을 권한다.

1. 코드·테스트: `src/ddak/app.py`, `src/ddak/core/{app_repository,candidate,store}.py`, `src/ddak/executor/service.py`, `src/ddak/cloud/infra/{bindings,foundation,runtime}.py`, `src/ddak/onprem/deploy/migrate.py`, 해당 변경 테스트 전체. foundation/main.tf는 주석 변경뿐이므로 2번에 포함한다.
   - `승인 기반 초기 인프라와 앱 저장소를 연결하고 배포 준비·마이그레이션을 보완`
2. 문서·벤치마크: docs/, single-app/dev-docs/, harness/AGENTS.md, harness/docs/, foundation/README.md·main.tf 주석.
   - `후속 수정 3·4의 운영 기준과 회귀 검증 및 팀 연동 요청을 정리`

Git index는 건드리지 않았다. 사람이 diff 검토 후 커밋·push한다. 앱 레포 상태/dev-only는 사용자가 제공한 정보이며 이번에 원격을 다시 조회하지 않았다.
