# 후속 수정 5 인계 — 2026-10-02

기준 HEAD `87370df`. 작업 사본만 수정했다. stage·commit·push·PR, AWS/VM 실행은 하지 않았다. 외부 팀 코드(plan/web/cloud build/deploy/verify)는 변경하지 않았다. 아래 DONE은 이번 정준우 범위의 로컬/fixture 검증이며 실제 전체 E2E 완료가 아니다.

| 항목 | 구현 결과 |
|---|---|
| 최우선 PR #8 | E2E에 통과용 scanner 명시 주입, 최초/재시작 동일 적용, source=fixture 기록. 제품 scanner fail-closed 유지 |
| 1 | 부분 빌드 때 미변경 tier의 이미지·원래 릴리스·artifact를 환경 장부에 보존. v1 전체→v2 was→실패 롤백→전체 배포 회귀 |
| 2 | FAKE 저장소 후보 생성/게시 구현, Git 쓰기 없음, REAL origin 검사 유지 |
| 3 | 승인 source_binding을 build context로 전달, 사전 지정/refresh 변조/다른 artifact snapshot 차단 |
| 4 | codebuild_project_name/image_repository 등 허용 플랫폼 출력의 저장·다음 요청 주입, ARN 보존, fake/real 분리 |
| 5 | state 버킷/설정과 unknown bucket 차단, 앱 buildspec 기본 실행 차단, StartBuild override 정책 설계 |
| 6 | generate_infra 계약→번들 해시 검사→InfraBinding→validate/plan/승인/apply 조립. 실제 생성기 구현은 O2 몫 |
| 7 | local state 불명확 실패 시 project/layer 복구 표식·경로 보존, 다른 run도 차단 |
| 8 | Git HTTP redirect 금지, tag clone 전에 intake scheme/host 확인 |
| 9 | repository 없음/origin 오류를 FAILED+phase/code/detail로 기록 |
| 10 | migration 요청 버전 precheck, bucket 생성 증거/즉시 태그, 별도 state 이전 예산 |
| 11 | C1/O1 가이드·결정·역할 문서 정정, 소유자 요청 분리 |

검증 로그는 `harness/var/validation/followup5-20261002/`(Git 제외), 최우선 CI 교정은 `harness/var/validation/ci-scanner-fixture-20261002/`에 있다. 시작 UTC·총 소요·종료 코드를 JSON으로 보존한다. 수치는 fixture와 로컬 Git 비용이며 실제 배포 벤치마크로 쓰지 않는다. 최종 CI: 1196 passed / 1 skipped / 4 deselected, 89.150초. gitleaks 없는 전체 테스트: 1194 passed / 3 skipped / 4 deselected, 86.955초. lint/type/boundary/contracts 통과.

## 담당자 요청 목록

- **서윤(C3), 우선:** 준비 실패 run의 approvals 라우트에서 서비스 PRECONDITION_FAILED를 4xx로 변환(현재 HTTP 500). 설정 저장은 공개 `save_project_settings(..., expected_version)` 사용. 시작 버튼은 `await service.request_deployment(project, targets=None|onprem|cloud|both, ref=None|감시브랜치|v태그)` → run_id. 설정 repo_url/watch_branch/default_targets/auto_detect, 온프렘 전용 cloud_domain 선택 입력, result.phase/code/detail/missing_tool·SSE 연결은 기존 요청 유지. C-18 headline의 HCL source를 그대로 표시한다.
- **승환(C2):** build_image tool.py를 `build_tier(GitSource(ctx.repo_url, ctx.candidate_sha), current=ctx.release_artifacts, RELEASE_ID=ctx.run_id, snapshot=ctx.source_binding)`에 연결. FAKE에서는 FakeCodeBuild. sourceVersion=candidate_sha, 빌드 결과 commit SHA·이미지 라벨. `ctx.platform.cloud.codebuild_project_name`과 `.image_repository` 사용, StartBuild IMAGE_REPO는 4번째 PLAINTEXT override. 코드 소유 buildspecOverride 필수(프로젝트의 기본 buildspec은 실패 전용). S3 대체 경로는 정확한 sourceLocationOverride를 정책에도 반영하고 전용 버킷 읽기 권한 검토. 프로젝트 environment_variable 금지는 유지. 레지스트리 자격 공급 연결은 별도 수동 UI 입력 없이 코드로 하며 값은 AI/로그에 넣지 않는다.
- **준석(O2/C1):** GenerateInfraInput/Output 계약에 맞는 generate_infra 툴만 구현/등록. 조립·세션·영속 runtime은 O1 구현이다. 출력 이름 `codebuild_project_name`, `image_repository` 필수(후자는 Docker Hub namespace/repository 문자열), source.buildspec은 `OVERRIDE_REQUIRED_BUILDSPEC` 사용. 입력 directory에 직접 하위 .tf만 쓰고 파일 SHA-256·layer·outputs·source 반환. code-owned state bucket/설정은 생성하지 않는다. 생성기는 ctx.deadline을 지켜야 한다. 기존 요청: watch.py main→prod, 계획 source_sha, migration 실제 DB 판정, MIGRATE_MODE/APP_ENV 내부 기본값 분류, annotated tag peel 순서, TLS 인증서 오류를 transient로 분류하지 않기, 없는 ref에 요청명/브랜치 진단.
- **민영(O3):** smoke_test/compare_env_results 등록·출력 계약과 앱 실제 로직 연결. 재사용 패치가 새 prod에 맞지 않으면 패치 단계에서 재제안. .env.example 허용/.env 금지 유지.

## 남은 제약과 복구

- 실제 generate_infra·CodeBuild·ECS·전체 AWS 부트스트랩은 이번에 실행하지 않았다. 실제 툴 연동 뒤 재검증해야 한다. 3분/항상 성공을 주장하지 않는다.
- 기본 조립은 bootstrap=platform/update=app 한 층씩이다. 플랫폼 개선 층 선택, 앱 층의 마지막 HCL 보관/재사용, 승인 대기 infra binding의 재시작 복구는 추가 통합 항목이다. 일반 승인 대기 실행 DB 복원과 달리 프로세스 내 InfraBinding은 재생성 없이는 복원되지 않는다.
- FAKE generate_infra 등록은 제품에 넣지 않았다. 생성기가 없으면 이름을 포함해 실패하며, FAKE 인프라에는 명시적 fixture binding factory가 필요하다.
- C-18은 HCL source 표시를 포함한 최종 JSON 전체가 8 KiB 이하여야 한다. 초과하면 승인을 거부하며 IAM diff를 임의로 잘라내지 않는다.
- 승인 전 생성 타임아웃은 번들을 격리하고 적용을 막는다. 동기 생성 스레드를 강제로 종료하지는 않는다. 생성기 자체 deadline 구현은 O2 요구사항이다.
- local state 실패 복구: bootstrap-recovery JSON의 run/work_dir/local_state_path와 생성 버킷 증거를 보고 계정/대상/원격 state를 사람이 확인한다. 확인 없이 표식 삭제·재실행·bucket 삭제·state 덮어쓰기를 하지 않는다. 정상 remote 이전이면 표식이 자동 제거된다.
- StartBuild IAM의 buildspec 조건은 override 존재만 확인한다. 임의 문자열 내용을 IAM이 검증하는 것으로 설명하지 않는다. 정확한 buildspec은 코드 소유 C2 어댑터가 고정한다.
- 기존 추가 요청(worktree 정리, prod 이력 재작성, Terraform 경계 일치/추가 출력 타입 범위, VM 앱 DB 이름 결정)은 계속 남긴다.

## 제안 커밋 순서

1. `test: inject deterministic secret scanner into deployment roundtrip` — `harness/tests/e2e/test_g_roundtrip.py`만. CI 긴급 교정 독립 분리 가능.
2. `fix: preserve tier releases and connect approved build and infra inputs` — src의 이번 변경 + 나머지 테스트 + generate_infra 스키마. foundation/main.tf 주석은 다음 문서 커밋에 포함해도 된다.
3. `docs: update follow-up 5 contracts and integration handoff` — harness/docs·dev-docs·infra README/참고본 설명.

중간 커밋별 CI는 이번에 별도 사본으로 재검증하지 않았다. 사용자가 diff 검토 후 커밋한다. AI attribution을 넣지 않는다.

비밀값 검사: 변경 파일 사본은 통과했다. 전체 디렉터리는 기존 provenance 해시·pytest 캐시/pyc·검증 메타데이터에서 31건 검출되어 전체 통과로 표기하지 않는다. 이번 수정에 없는 파일들이며 예외 추가/규칙 비활성화는 하지 않았다. metadata만 `full-tree-secret-findings-metadata.json`에 기록.

마감 시 다른 세션의 커밋 `9ff8b3f`(코드/테스트), `bd8d639`(문서)를 확인했다. 위 3개 메시지는 제안 이력이며 다시 커밋할 필요가 없다. 현재 미커밋은 최종 검증 수치를 보완한 이 문서와 O1 작업 기록 2개다. 추가 제안: `docs: record final follow-up 5 validation results`.
