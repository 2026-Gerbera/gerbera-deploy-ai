# 10/2 야간 추가, 아침 검토 필요

정준우 야간 지시와 보충 지시를 기준으로 한다. 이전 직렬 배포·대기 지점 문구보다 우선한다.
커밋·push·PR, VM 재접속, 실제 AWS/Terraform/레지스트리 쓰기는 하지 않는다.

## 1. 독립 실행

- 카탈로그에서 환경 간 완료 대기를 제거했다. 완료 SignalName 두 값은 유지한다.
- 이미지가 필요한 DB·앱 배포는 `images_ready`를 명시적으로 기다린다. 인프라 준비는 빌드보다 앞설 수 있다.
- 실행기는 승인한 wait_for를 그대로 실행하며 환경 간 검증 대기가 남은 구형 계획은 PLAN_INVALID로 거부한다. 새 계획과 승인이 필요하다.
- 한 환경 실패/복구 실패 중 다른 환경은 진행한다. 성공 환경의 릴리스 상태는 SUCCEEDED를 유지한다.
- 양쪽 모두 실제 트랙이 있고 성공한 경우에만 교차 검증한다. 불일치 때는 기존 결정대로 클라우드만 복구한다.
- 공유 빌드/읽기 작업이 진행 중이라는 이유만으로 환경 복구를 막지 않는다. 종료 불명 작업과 같은 대상의 진행 중 변경은 계속 복구를 차단한다.
- 독립 검토 1회: 빌드 의존 상실, 공유 작업 중 복구 거부, 빈 트랙 교차 검증 지적을 확인하고 수정했다.

## 담당자 요청 목록

- 준석 확인 필요(야간 최소 수정): `plan/validate/assemble.py` 조립부 및 `_step`의 `has_local` 조건부 local_verified 삭제를 제거했다. 이제 카탈로그 의존을 그대로 복사한다. `plan/*`의 다른 생산 코드 수정은 없음.
- 준석: 이미 저장된 구형 계획은 재계획해야 한다. 변경 탐지/감시 연동은 별도 소유 범위다.

## 2. 대상·트리거 (야간 추가, 아침 검토 필요)

- 선택 필드 `RunContext.targets=None|onprem|cloud|both`, `trigger=manual|auto` 추가. None은 기존 계획 대상 유지.
- 승인 전 선택 대상에 맞게 계획을 복사해 투영한다. 원본 해시를 검사하고 투영한 계획을 다시 승인한다.
- 제외 환경의 단계·환경 지정 finally까지 호출하지 않으며 상태는 N/A, 환경 릴리스 행은 보존한다.
- 선택한 대상에 계획이 없으면 거부한다. refresh가 대상·트리거를 바꾸면 실행을 실패시킨다.
- 독립 검토 1회: 제외 환경 finally 호출과 refresh 메타 변경을 교정하고 회귀 테스트를 추가했다.

## 3. Git 배포 기록 (야간 추가, 아침 검토 필요)

- `RunContext.source_sha`, `candidate_sha` 선택 필드를 추가했다. 후보 SHA를 주면 원본 SHA도 필요하다.
- 릴리스·환경별 성공 기록에 두 SHA를 남기며 `deployment_baselines(project)`는 마지막 성공 source_sha만 반환한다.
- 운영자가 연결한 AppRepository만 게시한다. FAKE에서는 명시적인 로컬 저장소 시험 연결만 허용한다.
- 성공 환경의 `deployed/onprem`, `deployed/cloud` 태그만 이동한다. 움직이는 태그에만 기존 OID를 명시한 lease와 atomic push를 쓴다. ai-prod/main 브랜치 강제 갱신은 없다.
- 선택 대상 전체 성공 때만 main을 fast-forward한다. 태그 성공 후 main 충돌이면 PARTIAL로 기록하며 환경 배포 결과는 보존한다.
- Git 명령·게시 총 소요 시간을 릴리스 git 필드에 기록한다. 반복 취소도 진행 중 게시가 끝날 때까지 기다린다.
- 독립 검토 1회: 게시 예외가 배포 결과를 지우는 문제와 두 번째 취소 때 작업이 남는 문제를 교정했다.
- C2 요청: 현재 `cloud/build/codebuild.py`는 S3의 VersionId만 sourceVersion에 전달한다. candidate_sha로 Git 소스를 고정하는 연결은 승환 담당 수정이 필요하다(야간 소유 범위 밖).

## 4. 후보 생성 (야간 추가, 아침 검토 필요)

- 운영자가 연결한 앱 전용 checkout에서 원격 prod를 fetch하고 승인 source_sha의 계보·원본 manifest를 확인한다.
- 격리 worktree에서 기존 ai-prod 이력에 prod SHA를 merge한다. 승인 트리를 고정한 merge 커밋 하나 생성 전 검사, staged·작업 사본·커밋 트리 대조 후 고정 후보 SHA를 일반 push한다. 사용자 Git 신원을 그대로 사용한다.
- supplied candidate_sha도 원격 prod/ai-prod 계보·manifest·비밀검사를 빌드 전에 거친다.
- 최초 구현은 충돌 시 재패치 제안으로 중단했으나 **수정 라운드 1에서 폐지**했다. 아래 §8의 승인 트리 확정 방식이 현재 기준이다. 기존 conflict 조회 API는 후속 수정 2에서 제거했으며 get_release의 merge_conflicts를 사용한다.
- 취소는 다음 커밋/push 전 guard에서 차단한다. 이미 실행 중인 Git 명령은 종료를 확인하고 `candidate-attempt.json`, 완료 후보는 `candidate.json`에 남긴다. 빌드는 시작하지 않는다.
- 독립 검토 1회에서 supplied 후보 검사 누락, HEAD push 경쟁, 생성 중 취소를 지적받아 교정했다.
- 기본 비밀검사는 Gitleaks의 전체 작업 트리 검사다. 코드 소유 설정·빈 ignore 목록·allow 주석 무시를 사용한다. 미설치·오류·의심 항목 모두 차단한다. 이 Mac에는 gitleaks가 없어 실제 탐지율은 검증하지 않았고 로컬 Git 테스트는 명시적 검사 fixture를 썼다.
- CLI 옵션 근거: https://github.com/gitleaks/gitleaks#usage

## 5. WP11 내부 API와 설정 (야간 추가, 아침 검토 필요)

- 공개 Python API: `get_run`, `list_runs`, `get_release`, `get_environments`, `get_approvals`, `get_project_settings`, `save_project_settings`. 기존 `events`, `subscribe`, `approval_view`는 유지한다.
- repo_url, watch_branch(기본 prod), auto_detect, default_targets, cloud_domain/dns_mode/hosted_zone_id를 검증한다. 설정 저장은 읽은 버전이 필요하며 생략한 기존 필드를 보존한다.
- prepare에서 RunContext.project_settings 선택 필드에 설정과 버전을 복사한다. 이미 준비한 run은 이후 설정 변경의 영향을 받지 않는다. C-18/패치 메타는 approval_view로 전달한다.
- 과거 실행은 신규 export가 없어도 기존 승인·plan·패치·릴리스에서 조회한다. 복원 불가능한 필드는 unavailable_fields로 표시한다. 재실행/재승인은 자동 복원하지 않는다.
- 독립 검토 1회: 도메인 전용 입력의 단일 대상 호환, 과거 기록 조회, 버전 누락 덮어쓰기를 교정했다.
- 서윤 요청: 화면 코드의 `.store.run/list_runs/environments/approvals/project_settings/save_project_settings` 호출을 대응 공개 API로 교체. 웹 화면·템플릿은 야간에 수정하지 않았다. 이 내부 API는 인증·인가를 대신하지 않는다. patch 원문 표시에는 기존 이스케이프·redaction을 유지한다.
- 준석 요청: 앱 조립부는 저장된 project_settings의 watch_branch를 Watcher에 공급 중이다. watch.py standalone 기본 main→prod 정정과 source_sha/targets/trigger 취합은 준석님 요청으로 유지하며 watch.py는 이 문서 작업에서 수정하지 않았다.

## 6. C1 연결 (야간 추가, 아침 검토 필요)

제품은 `validate_infra` → `plan_infra` → 한 화면의 infra 승인 → foundation(state bucket + app/build 권한 경계) → platform apply 순서로 실행합니다. bucket이 없으면 local backend plan을 승인한 뒤 코드가 SDK로 bucket을 만들고 platform local apply 후 remote backend로 state를 이전합니다. 사람이 미리 foundation/platform을 apply하는 전제는 폐기합니다. 사람의 사전 준비는 AWS 자격증명과 도메인 구매입니다. AI 개발 에이전트는 Terraform을 직접 실행하지 않으며, 승인 뒤 제품 코드가 실행하는 경로와 구분합니다. 실제 생성기의 O2 연결은 아직 미완이고 AWS 전체 완료를 의미하지 않습니다.

💭 v2 기능은 미정이며 로그인은 후보입니다. 정준우의 10/2 검증은 1차 Flask 기본 앱에 이미지·박스를 추가한 파이프라인 E2E, 2차 실제 로직의 LLM 분석·패치·인프라 생성 검증으로 나눕니다. 1차 결과로 실제 LLM·AWS 전체 완료를 주장하지 않습니다.

- `terraform/foundation`에 state 버킷·앱/빌드 권한 경계 2개를 추가했다. 현행은 제품 infra 승인 뒤 코드가 적용하며 사람 사전 apply 전제는 폐기했다. 기존 SDK foundation 경로와 중복 적용하지 않는다.
- `validate_infra`, `plan_infra`, `apply_infra` 모델·등록과 run별 `InfraBinding`을 추가했다. validate/plan은 승인 전, apply는 승인된 계획에서 실행한다. FAKE는 명시적 fixture runner가 필요하다.
- RunContext refresh는 platform/app 출력 허용목록·타입·sensitive를 검사한다. 앱 시크릿은 `app_secret_arn_<KEY>`를 쓰며 기존 `secret_arn`은 호환용으로만 유지한다. flat/nested 기존 TLS·온프렘 설정을 보존한다.
- `DdakToolError.needs_human`은 기본 False인 선택 인자다. Terraform 시작 뒤 적용/출력 확인 실패는 True로 전달하고 실행기가 앱 롤백으로 완료 처리하지 않는다. 실패한 cloud와 잠금은 NEEDS_HUMAN으로 남으며 성공한 local 기록은 유지한다.
- `ensure_tls`는 인증서 검증을 켠 TLS 1.2 이상 HTTPSConnection으로 443 HEAD 응답까지 확인한다. HTTP 오류 응답은 TLS 연결과 구분하며 앱 기능/HSTS 검증은 C3 범위다.
- CD ensure_tls 툴은 app 조립부가 C1 공개 함수를 주입받는다. C2 AwsProvider 파일은 수정하지 않았고 그 직접 메서드는 아직 미구현이다. `apply` 모드는 계속 거부한다. 가짜 출력에는 fixture 라벨을 붙인다.
- 독립 검토 1회에서 Terraform 부분 실패를 앱 롤백 완료로 오판하는 문제와 flat 출력 호환 문제를 발견·교정했다. 서비스 전체 경로로 apply 실패/출력 실패·잠금 유지·반대 환경 성공을 검증했다. TLS 등록 뒤 순환 import도 교정했다.
- `contracts-update`, Terraform fmt/정적 HCL 검사, 전체 CI **1,013 passed, 1 skipped, 4 deselected / 45.2초** 통과. 실 AWS·Terraform init/plan/apply·실제 443 접속은 실행하지 않았다.
- 준석 요청: generate_infra C-20 파일/변수/출력 주소 계약과 run별 실 세션 조립이 미연결이다. 초기 foundation/platform은 한 화면 infra 승인 뒤 제품 실행, 앱 개선 배포는 별도 run이다. 후속3에서 첫 플랫폼은 승인 전에 app 조립이 apply→infra_ready→build/TLS 순서를 구성한다. 실제 생성기 binding 연결 검증은 남아 있다.
- C2/C3 요청: CodeBuild Git sourceVersion, AWS 배포 구현, TLS/HSTS·앱 기능 검증을 취합한다. 카탈로그 ensure_tls의 기존 변경 영향/2700초 상한은 보수적인 옛 메타이며 확인 전용 의미에 맞춘 정리는 별도 공유 변경 요청이다.

### 작업4 검증 보완 (문서 정리 중)

로컬 Gitleaks 8.30.1을 Homebrew로 설치했다. 실제 CLI로 깨끗한 fixture 통과(0.383초),
가짜 형식 토큰 및 앱측 allow 설정/주석이 있는 fixture 차단(0.022초)을 확인했다.
실제 자격증명은 사용하지 않았고 검사 출력/원문 토큰을 기록하지 않았다. 로컬 bare Git 후보 경로의
실제 scanner 테스트도 추가했다. 앞 절의 미설치는 작업4 완료 시점 기록이며 현재 Mac에는 설치돼 있다.
다른 실행 호스트(WSL/발표용)는 별도 준비가 필요하고, 이 사례로 모든 비밀값 탐지를 보장하지 않는다.

## 7. 문서와 마감 (야간 추가, 아침 검토 필요)

O1/C1/3티어/완료 가이드와 인계 문서, 코드 README, 10/1 결정의 후속 링크를 갱신했다.
문서 독립 검토 PASS. 초기 작업7 CI는 테스트 통과·lint S311 실패였으며 가짜 토큰 생성식 교정 뒤
최종 전체 CI **1,015 passed, 1 skipped, 4 deselected / 45.7초** 통과했다.
기능 미연결/실측 범위를 명시했고 실제 VM 수치는 야간 변경 전 증거로 유지한다.
`docs/benchmarks/2026-10-02-night`에 CI 실패/성공 CSV·JSON·로그 해시·마감 UTC/시간을 보존했다.
소유 범위 대조 결과 다른 담당 생산 코드 변경은 허용한 assemble.py 최소 수정뿐이다.
Git branch는 o1/onprem-three-tier, stage/commit/push/PR 없음. 아침 후속은 인계 문서를 따른다.

## 8. 수정 라운드 1–9 (10/2, 아침 검토 필요)

- **승인한 트리가 배포 트리다.** prod merge의 충돌 파일은 candidate-attempt.json 기록용이며 중단하지 않는다.
  관리 대상 파일을 승인 build_files와 일치하게 덮어쓰고 삭제한 뒤 manifest를 대조한다.
  ai-prod와 prod 이력을 부모로 유지한 merge 커밋을 만들고 검사한 고정 SHA를 일반 push한다.
  최초 ai-prod가 없어서 두 SHA가 같은 경우 부모는 하나다. Git이 무시하는 승인 파일도 명시적으로 stage한다.
- **정준우 확인 필요:** 패치 OFF의 승인 트리는 prod 그대로이므로 이전 AI 패치가 제거된다. 승인하지 않은
  이전 수정이 자동으로 배포 후보에 남지 않게 한 의도된 동작이다. 재사용 패치의 새 prod 적합성 판단/재제안은 민영님 요청이다.
- `.env.example`·`.env.sample`은 관리 대상/승인 비교에서 제외하고 prod 내용을 그대로 유지하되 비밀검사에는 포함한다.
  `.env`·*.pem·*.key 및 기존 지원하지 않는 파일은 계속 fail-closed다.
- rollback_tier는 이번 run에서 deploy_tier/prepare_db가 실제 호출된 tier만 복구한다.
  infra/TLS/env 준비만으로 앱 롤백을 실행하지 않는다. 빌드 실패가 먼저면 FAILED_BEFORE_DEPLOY이며
  배포 미시작 환경은 SKIPPED다. 이미 적용한 인프라는 infra_changes에 남기며 refresh 실패에도 보존한다.
- compare_env_results/diagnose_parity_gap 실패만 PARITY_FAILED다. 환경별 verify 실패는 그 환경 실패이며
  다른 환경 검증은 계속한다. 조건 미충족 compare는 STEP_SKIPPED로 남긴다. 공통 비동등성 검증 실패는 FAILED_VERIFY.
  양쪽 배포 실패는 대표 상태 FAILED_LOCAL과 두 track 결과로 표현한다. 복구 실패/종료 불명은 NEEDS_HUMAN이 우선한다.
- 443 probe는 DNS를 포함해 monotonic 기준 min(10초, 남은 시간)으로 제한하고 timeout은 불합격(False)이다.
  registry STATE_CHANGE/lock/2700초는 변경하지 않았다. AWS SDK 전체 자격증명/DNS 과정의 강제 종료를 보장하는 것은 아니다.
- targets=None은 원래 계획 대상 유지다. default_targets는 O2/C3 접수 시 명시적으로 RunContext에 넣는다.
  prepare 승인 스냅샷은 실제 저장 필드/버전만 포함하며 legacy 설정 저장도 전달하지 않은 필드를 유지한다.
- 공유 RunContext/스키마 필드 추가는 이번 수정 라운드에는 없다. RunResult/릴리스의 infra_changes와
  실행기 FAILED_VERIFY 결과가 추가되어 C3에 인계한다. 야간 스키마 추가는 코드와 같은 2번 커밋에 포함한다.
- 사용자 지정 나머지 P3(worktree 정리, prod 이력 재작성, list(string) 원소 검사, Terraform 경계 일치 테스트)는
  구현하지 않았다. 요청·검증·커밋 계획의 최신 기준은 [수정 인계](../guides/O1-night-handoff-2026-10-02.md)다.

## 9. 후속 수정 2 — 야간 추가, 아침 검토 필요

- 교차 검증 실패 시 cloud가 DONE이면 실제 앱 변경 여부와 무관하게 성공으로 남기지 않는다.
  앱 변경이 있으면 rollback, 없으면 FAILED다. 따라서 실패한 cloud 태그와 main은 이동하지 않고
  성공한 onprem 태그만 이동한다. 실제 원격 호출 없이 로컬 bare Git으로 검증한다.
- 롤백 시간 예산은 deploy_tier와 prepare_db의 tier를 합쳐 중복 제거한다. prepare_db의 생략 tier는
  실제 실행 추적과 같은 was다. 적용한 DB 변경의 역마이그레이션을 뜻하지 않는다.
- prod SHA의 deploy.yaml env_example 경로도 템플릿으로 취급한다. 제외 부모와 정확히 .env인 이름은
  허용하지 않는다. .ENV·*.KEY·*.PEM도 후보 tree_manifest에서 대소문자를 무시해 차단한다.
  snapshots.py는 변경하지 않았다. 템플릿은 prod 내용 보존·비밀검사 포함 원칙을 유지한다.
  공용 스냅샷에 포함되는 일반 경로 템플릿이 승인 원본/수정본에서 prod와 다르거나 삭제되거나 prod에 없는 파일을 추가하면
  후보 생성과 supplied 후보 검사를 거부한다. 승인 build-source와 candidate_sha의 내용이 다른 채 통과하지 않도록 했다.
- 쓰이지 않는 CandidateConflict, get_conflict_proposal, reused 응답 필드를 제거했다.
  새 릴리스/실행 결과에 선택 데이터 merge_conflicts를 추가하고 get_release로 읽는다.
  후보 생성 완료 시 관측한 충돌 파일 목록이며, 기존 기록에 없으면 소비자는 빈 목록으로 취급한다.
  C3·O3는 삭제한 재패치 API 대신 이 목록을 사용한다. 스키마/RunContext/카탈로그 변경 없음.
- 앱 저장소의 prod는 PR merge로 코드 반영하며 직접 push·force push·삭제를 막는다.
  main·ai-prod는 파이프라인이 직접 push하므로 보호하지 않고, deployed/*는 lease 이동을 위해 태그 보호를 걸지 않는다.
  이번 작업은 문서 정정만이며 실제 GitHub 설정은 바꾸지 않았다. 데모: v2 PR을 prod에 merge.
- 준석님 기존 watch.py가 감시(main → prod 변경 요청). git 충돌은 승인 트리로 해결해 멈추지 않으며
  재사용 패치가 새 prod에 맞지 않으면 승인 전 패치 단계에서 민영님이 재제안한다.
- 기존 3개 커밋은 사용자 반영 완료. 이번 제안은 코드·회귀 테스트 1개와 문서·벤치마크 1개 커밋이다.
  에이전트는 stage·commit·push·PR 및 외부 VM/AWS/GitHub 접속을 하지 않는다.
