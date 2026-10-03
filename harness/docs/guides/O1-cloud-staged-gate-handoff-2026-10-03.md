# 대회 rolling 게이트와 배포 결정 정정 인계 — 2026-10-03

상태: **BLOCKED — 전체 CI의 로컬 소켓 권한 제한**. 14:35 rolling 작업 뒤 로그 그룹 범위 수정과 회귀 검증을 진행했다. 전체 CI의 HTTP 테스트 서버가 샌드박스에서 바인딩을 거부당해 요청한 `0 failed`를 충족하지 못했다. 아래 후속 절의 최신 결과를 따른다. 실제 AWS 배포 리허설은 별도다.

## 작업 위치와 기준

- 작업 트리: .worktrees/cloud-staged, 브랜치: o1/cloud-staged-gate.
- git fetch origin main cloud 후 최신 origin/main 36933353eda375a53a52367739ed66a9ec9844e3에서 새 작업 트리를 만들었다. PR #21 merge 커밋이 기준이다. 이전 cloud-int나 원 작업 트리는 수정하지 않았다.
- .venv는 ../../.venv를 가리키는 심볼릭 링크만 만들었다. 의존성 동기화·공유 가상환경 연결 변경은 하지 않았다. 검사는 UV_NO_SYNC=1과 이 작업 트리의 src를 PYTHONPATH로 지정했다.
- 커밋·push·PR·태그·git config 변경은 하지 않았다. 실제 AWS·Terraform·VM·Docker·claude 호출은 하지 않았다.

## 변경 코드와 테스트

- src/ddak/cloud/infra/policy.py: rolling 분기·명시 비율 검사·서비스 전략에 따른 기존 listener 검사 적용.
- src/ddak/cloud/infra/plan.py: 검증한 after/unknown을 이용해 같은 전략 범위를 적용.
- harness/tests/unit/cloud/infra/test_staged_deployment_policy.py: rolling 회귀 33개.
- harness/tests/unit/cloud/infra/test_bluegreen_policy.py: 이제 허용하는 rolling·전략 생략의 과거 거부 기대를 잘못된 전략·블록·null 거부로 정정. 기존 BLUE_GREEN 통과/위반 검사 내용은 보존.
- 14:35 rolling 작업에서는 providers·foundation·ECS 배포 코드·서윤님 prompt.md·공유 계약·의존성 파일을 변경하지 않았다. 이후 로그 범위 수정은 아래 후속 절에 별도로 기록한다.

## 최종 결정과 구현

- **대회(10/4)는 클라우드·온프렘 모두 rolling이다. 블루그린은 대회 뒤다.** 오전의 처음부터 블루그린 결정과 14:20 단계 전환 결정을 14:35에 뒤집었다. C2·C3의 rolling 개발을 활용하고 세 담당의 하루 내 전환 위험을 피한다.
- 게이트는 전략 생략 또는 ROLLING을 허용한다. ECS controller, 회로 차단기 enable+rollback, 기존 서비스 task_definition/desired_count 소유권 검사는 유지한다. CODE_DEPLOY·EXTERNAL과 미확정 전략은 거부한다.
- rolling의 deployment_minimum_healthy_percent·deployment_maximum_percent는 생략할 수 있으며, 명시하면 각각 정수 100·200이어야 한다. 현재 코드에는 별도 비율 검사가 없었으므로 기존 프롬프트의 100/200 지시를 기준으로 추가했다.
- TG 해제 지연 ≤30초, 헬스 간격 ≤10초, healthy_threshold ≤3은 방식에 관계없이 유지한다. 변경 없는 리소스(no-op)도 검사한다.
- rolling에는 TG 2개·listener rule·advanced_configuration을 요구하지 않는다. rolling 서비스와 함께 있는 단일 TG listener rule도 허용한다. 여러 HCL 파일을 모두 읽은 뒤 서비스 전략에 맞게 listener 검사 범위를 정하고 plan에서도 같은 판정을 적용한다.
- 기존 BLUE_GREEN 전용 검사는 휴면 분기로 보존한다. 역할·bake·advanced configuration·hook·listener의 기존 검사를 삭제하지 않는다. 전략/소유 관계를 확정할 수 없는 독립 listener는 기존 엄격 검사를 유지한다. 새 블루그린 기능·검사·회귀 확대는 중단했다.
- 기존 providers 허용 리소스와 foundation의 ECS 인프라 역할 생성은 그대로다. 미사용 역할 생성을 BLUE_GREEN일 때만 실행하도록 바꾸는 것은 대회 뒤 정리 후보다. ECS 배포 실행 코드는 수정하지 않았다.

## 서윤님 최신 프롬프트 읽기 대조

대상은 origin/cloud의 c4af8656c2a6b574311f37fe3f12d0272c99e66d이며, prompt.md의 마지막 변경도 같은 커밋이다. 파일은 src/ddak/cloud/infra/tools/generate_infra/prompt.md다. git show로만 읽었고 파일을 가져오거나 고치지 않았다.

| 프롬프트 위치 | 실제 문구·값 | 새 게이트와의 대조 |
|---|---|---|
| 49~51행 | TG 하나, deregistration_delay = 30, HTTP redirect와 HTTPS forwarding | rolling 구성·해제 지연 상한과 맞음 |
| 61~62행 | deployment circuit breaker with rollback, rolling deployment percentages 100/200 | rolling 방향·비율과 맞음. 실제 enable/rollback bool과 ECS controller는 산출물 검사에서 확인해야 함 |
| 파일 전체 | health_check.interval 및 healthy_threshold 값 지시 없음 | interval ≤10, healthy_threshold ≤3 준수를 보장하지 못함 |
| 49~62행 | BLUE_GREEN·TG 2개·advanced_configuration 지시 없음 | 대회 rolling 방침과 충돌하지 않음 |

**대조 결과: 프롬프트의 rolling 자체는 더 이상 게이트 거부 이유가 아니다. 다만 헬스 수치가 누락되어 생성 결과의 전체 통과를 보장할 수 없다.** health_check 블록 또는 값이 빠지거나 상한을 넘는 생성물은 계속 거부된다. C3 후속은 rolling 프롬프트에 interval ≤10·healthy_threshold ≤3을 명시하는 것이다. 실제 생성·Terraform 실행 검증은 하지 않았다.

## 문서 정정

- 중앙 결정 기록 첫 줄에 14:35 번복 상태를 명시했다. 오전의 처음부터 블루그린·트래픽 전환 완료·bake 1분 문장은 지우지 않고 철회 표시를 붙였다.
- 오전 → 14:20 → 14:35 변경 순서와 일정 위험을 기록했다. 10/2 결정 4의 rolling 문구는 유효(대회 기준)로 복원한다.
- 온프렘도 대회는 rolling이다. 추가형 마이그레이션·단일 전환 지점·헬스 뒤 전환·설정으로 방식 선택은 대회 뒤 블루그린 준비 조건으로만 기록했다.
- 전수 대조 범위: AGENTS.md, harness/AGENTS.md, harness/README.md, harness/docs/decisions의 10/2·10/3 기록, harness/docs/guides의 O1·O2·runbook·인계서, single-app/dev-docs 전체. 과거 기록에는 대체 표시만 붙이고 당시 기술 내용은 보존한다.

| 정정 문서(저장소 루트 기준) | 최종 파일의 수정 줄 |
|---|---|
| AGENTS.md | 4~7 |
| harness/AGENTS.md | 90~92 |
| harness/README.md | 11~12 |
| harness/docs/ai-usage/O1.md | 696~703 |
| harness/docs/decisions/2026-10-02-team-status-and-decisions.md | 10, 26 |
| harness/docs/decisions/2026-10-03-cloud-bluegreen.md | 1~2, 6, 9~11, 13, 15, 23~39, 42~43, 73~75, 81~85 |
| harness/docs/decisions/2026-10-03-decisions-catch-up.md | 11~12, 37~38, 100 |
| harness/docs/guides/O1-cloud-integration-handoff-2026-10-03.md | 3~4, 92 |
| single-app/dev-docs/roles/00_10월2일-변경사항.md | 35~36 |
| harness/docs/guides/O1-cloud-staged-gate-handoff-2026-10-03.md | 1부터: 새 인계서 전체 |

O1·O2 가이드 및 runbook을 포함한 나머지 대상 파일은 검색 결과 현행 블루그린 강제 문장이 없어 불필요하게 고치지 않았다. single-app/dev-docs에서는 10/2 변경 요약의 과거 검토 시점에 대체 표시를 붙였다.

## 14:35 rolling 작업 검증 — 이전 결과

모든 Python 검사에 적용한 환경:

```sh
UV_NO_SYNC=1 PYTHONPATH=/Users/joonwoojung/Desktop/01_workspace/04_SoftBank_hackerton/.worktrees/cloud-staged/src
```

- 수정 전 인프라 회귀: 616 passed / 2.16초.
- 14:35 범위로 정리한 인프라 회귀: 649 passed / 2.08초. 추가 회귀 33개는 rolling 대상이다.
- 전체 CI: exit 0. lint/type/boundary/contracts/test 모두 PASS. **2477 passed, 1 skipped, 4 deselected**, pytest 178.66초, 총 181.4초. 실제 서비스 검사 4개는 기본 CI에서 제외되며 skip 1개는 별도 temp-box 프로젝트용 앱 테스트다. 비밀값 검사 결과는 아래와 같다.

| 검사 명령 | 결과 |
|---|---|
| make -C harness test ARGS='tests/unit/cloud/infra -q' | 649 passed, exit 0 |
| make -C harness ci | 2477 passed, 1 skipped, 4 deselected; 전체 품질 검사 PASS, exit 0 |
| gitleaks git --redact --no-banner --log-opts='origin/main..HEAD' . | exit 0, 발견 없음. 신규 commit 0개·0 bytes이므로 미커밋 변경 검증과 구분 |
| gitleaks git --pre-commit --redact --no-banner . | exit 0, tracked 변경분 발견 없음 |
| gitleaks dir --redact --no-banner <변경·신규 파일 복사본> | exit 0, 변경·신규 14개 파일 발견 없음. .venv·민감 경로 제외 |
| git diff --check | PASS |

- 위 make 명령은 모두 앞의 UV_NO_SYNC/PYTHONPATH 환경변수를 지정했다. 파일 검사는 최종 문서 갱신을 포함해 수행했다.
- 전수 문서 대조는 지정 범위 52개 파일이며, 그중 정정 문서는 새 인계서를 포함해 10개다. 경로·일치 줄은 doc-audit.json, 전체 변경 목록은 changed-files.json에 보존한다.
- 로그 위치: harness/var/validation/cloud-staged-20261003/. 추적하지 않는 검증 산출물이다.
- 최초 14:20 작업 중 인프라 검사 659개가 통과했으나, 14:35 중단 지시 후 새 블루그린 관련 테스트 확대분은 제거하고 rolling 회귀와 기존 블루그린 회귀만 남겼다.
- 사전 독립 검토에서 공통 검사 우회·listener 전략 범위·비율 검사 공백을 지적받아 반영했다. 사후 독립 소스·문서 검토는 PASS다. 검토자는 테스트를 실행하지 않았으며 부모가 수행한 CI 결과와 구분한다. 외부 Claude 호출은 사용자 금지로 수행하지 않았다.

## 로그 그룹 이름 불일치 후속 수정 — 10/3

- 문제: 앱 권한 경계는 `/aws/ecs/ddak-*:*`만 허용했으나 생성 프롬프트와 배포 대상은 `/aws/ecs/${var.project}`를 사용했다. 또한 기존 게이트에는 로그 Resource 접두사를 전용으로 검사하는 규칙이 없었다.
- SDK foundation은 `settings.project`를 전달해 `/aws/ecs/<project>:*`와 `/aws/ecs/ddak-*:*`를 함께 생성한다. 참조 Terraform도 `var.project`로 같은 범위를 만든다. Flaskr 이름을 코드에 고정하지 않는다.
- HCL은 `${var.project}`, plan은 실행 설정의 실제 프로젝트 이름으로 허용 접두사를 확인한다. 전체 와일드카드·다른 프로젝트·다른 계정/리전·허용/금지 Resource 혼합을 거부하며, `no-op` 정책에도 같은 검사를 적용한다. 기존 CodeBuild 로그 범위는 유지한다.
- 게이트가 허용하는 `<project>*`는 같은 접두사의 이름까지 표현할 수 있지만, 앱 권한 경계는 `<project>:*`로 제한한다. 기존 `ddak-*`는 공용 예외이므로 모든 프로젝트 간 격리를 제공한다는 뜻은 아니다.
- 정책 문서 변경은 foundation 및 결합 infra 승인 해시에 반영된다. 기존 경계가 새 문서와 다르면 `PRECONDITION_FAILED`로 중단하고 자동 교체하지 않는다. 고정 경계 이름을 사용하는 같은 계정의 다른 프로젝트도 불일치로 중단할 수 있다. 기존 AWS 경계의 전환 및 실제 배포 검증은 이번 수정에 포함하지 않았다.
- 블루그린 휴면 분기·ECS 배포 실행 코드·생성 프롬프트·공유 계약·의존성은 변경하지 않는다.
- 변경 파일: `src/ddak/cloud/infra/{providers/aws.py,foundation.py,policy.py,plan.py,terraform/foundation/main.tf}`, `harness/tests/unit/cloud/infra/{test_log_scope_policy.py,test_bluegreen_foundation.py,test_bootstrap_dbinit_policy.py,test_runtime.py}`, 중앙 결정 기록 및 이 인계서. 총 11개이며, 시작 전부터 있던 `.venv` 링크는 변경하지 않았다.

### 후속 검증 환경과 차단 원인

저장소 루트에서 아래 환경을 사용했다. 기본 uv 캐시 접근이 거부되어 캐시만 이 작업 트리의 검증 디렉토리로 옮겼다. 의존성 동기화는 하지 않았다.

```sh
export UV_CACHE_DIR="$PWD/harness/var/validation/cloud-log-scope-20261003/uv-cache"
export UV_NO_SYNC=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src"
make -C harness ci
```

- 수정 전 인프라 회귀: 649 passed. 최초 실패 재현은 foundation 범위 2개 assertion 실패, 로그 게이트 64 failed / 31 passed였다. 기존 `ddak-<project>-was` 변수형과 `ddak-existing_logs` 호환도 추가 재현해 8 failed / 119 passed를 확인했다.
- 첫 전체 CI: lint/type/boundary/contracts PASS, **2 failed, 2581 passed, 1 skipped, 4 deselected, 6 errors**, make exit 2. 테스트 156.42초, 전체 158.5초.
- 최종 코드 검증: `make -C harness test ARGS='tests/unit/cloud/infra -q'` → **779 passed, 0 failed**, exit 0, 2.19초. Flaskr·두 번째 프로젝트·기존 ddak 이름·CodeBuild 허용과 다른 프로젝트·전체 와일드카드·계정/리전·혼합 배열 거부를 HCL 및 plan(create/no-op)에서 검사했다.
- 최종 `make -C harness ci` 재실행: lint/type/boundary/contracts PASS, **2 failed, 2599 passed, 1 skipped, 4 deselected, 6 errors**, make exit 2. 테스트 162.19초, 전체 164.1초. 원문은 `ci-final.log`이며 첫 실행과 동일한 소켓 바인딩 제한이다.
- 실패·오류 8개는 모두 수정하지 않은 `tests/unit/verify/smoke/test_smoke.py`에서 `ThreadingHTTPServer(("127.0.0.1", 0), ...)`의 `socket.bind`가 `PermissionError: [Errno 1] Operation not permitted`로 거부된 결과다. 테스트 삭제·skip·마커 변경으로 숨기지 않았다. 이 실행 환경에서는 권한 상승 실행도 허용되지 않는다. 따라서 전체 CI 통과 및 작업 전체 완료로 보고하지 않는다.
- 로그 위치: `harness/var/validation/cloud-log-scope-20261003/`. 이전 rolling 검증 로그와 분리했으며 Git에 추적하지 않는다.

| 비밀값·차이 검사 | 결과 |
|---|---|
| 아래 `gitleaks git` 이력 명령 | exit 0, 187 commits / 약 7.15 MB, 발견 없음 (`gitleaks-git.log`) |
| `gitleaks git --pre-commit --redact --no-banner .` | exit 0, 추적 중인 미커밋 변경분 발견 없음 (`gitleaks-diff.log`) |
| `gitleaks dir --redact --no-banner harness/var/validation/cloud-log-scope-20261003/changed-files` | exit 0, 신규 테스트를 포함한 최종 변경 파일 11개 전체 사본에서 발견 없음 (`gitleaks-files.log`) |
| `git diff --check` | exit 0 |

이력 스캔에서는 읽기 금지 경로를 pathspec으로 제외했다. 작업 트리 스캔은 지정한 변경 파일 11개만 복사해 검사했으며 `.venv`와 민감 경로를 포함하지 않았다.

```sh
gitleaks git --redact --no-banner --log-opts='HEAD -- . :(exclude)**/.env :(exclude)**/.env.* :(exclude)**/.secrets/** :(exclude)**/*.tfstate :(exclude)**/*.tfstate.* :(exclude)**/.aws/** :(exclude)**/.ssh/** :(exclude)**/.claude/**' .
```

독립 사전 검토의 로그 검사 누락·no-op 누락·기존 정책 불일치 조건을 반영했다. 커밋·push·PR·태그·git config 변경과 실제 AWS·Terraform·VM·Docker·claude 호출은 수행하지 않았다. 완료 조건으로 남은 것은 로컬 소켓을 허용하는 실행 환경에서 동일한 전체 CI가 통과하는지 확인하는 일이다.

## 남은 확인

실제 rolling 리허설·배포 완료·3분 성능은 이번 로컬 검사로 검증하지 않았다. 프롬프트 헬스 수치는 아래 후속 변경에서 보완한다. 실제 클라우드 리허설은 후속이며 블루그린 작업은 대회 뒤까지 중단한다.


## 10/3 재개 — 승인 뒤 경계 정책 버전 갱신 + rolling 프롬프트

이 절은 위 로그 범위 수정 당시의 “기존 경계 불일치 시 자동 교체하지 않음”과 “프롬프트·공유 계약 변경 없음” 기록을 대체하는 현행 상태다. 시작 시 HEAD는 `a16bbb4`, 브랜치는 `o1/cloud-staged-gate`였고 `git status`·일반/인덱스 diff에는 `.venv` 링크 외 선행 변경이 없었다. 로그 범위 수정 위에 아래 파일 변경을 남겼다. 커밋·PR 생성은 정준우가 진행한다.

### 경계 변경 계약과 실패 처리

- foundation 준비 단계에서 두 정확 ARN의 기본 VersionId·정규화 문서 SHA-256을 읽고 승인 해시에 결합한다. 승인 화면은 해시와 Statement/Resource 추가·삭제를 보여 준다. foundation·infra 승인을 받지 않고 IAM 쓰기를 시작할 수 없다.
- foundation 전체 사전 검사와 각 정책 쓰기 직전에 다시 읽는다. 승인 이후 변경, 다른 ARN, 불완전한 버전 목록은 중단한다. 다른 정책의 사전 검사 실패로 부분 변경을 시작하지 않게 두 정책 전체를 먼저 검사한다.
- 동일 정책은 재사용, 미존재는 CreatePolicy, 다른 정책은 버전 5개 미만에서 CreatePolicyVersion(SetAsDefault=true)이다. 5개이면 중단하고 자동 삭제하지 않는다. IAM 클라이언트의 total_max_attempts=1로 SDK 내부 재시도도 차단한다. default 버전 조회 실패를 정책 미존재로 간주하지 않는다.
- `apply_infra` 출력에 선택 필드 `boundary_versions`를 추가했다(기본값 빈 목록). 각 행은 마스킹한 정확 ARN·이전/새 VersionId·created/updated/unchanged/unknown 상태다. `DdakToolError`는 동일한 증거를 전달하며 실행기가 `infra_changes`에 성공/부분 변경을 기록한다. 툴 입력·step 카탈로그·이벤트·설정 키 변경은 없다.
- 각 시도 직전 unknown 영수증, 응답 직후 실제 버전 영수증을 기존 run 시도 표식 옆 `*-foundation-boundary-<n>.json`에 0600으로 쓴다. 프로세스 중단 복구 근거이며, 같은 run을 자동 재시도하지 않는다. 영속화 직후 core/runtime의 내부 contextvar 콜백을 통해 워커 반환 전에도 실행기로 전달한다. 취소·타임아웃에서 이미 수신한 영수증은 영속 run 결과 및 결과 화면에 반영하고 NEEDS_HUMAN으로 종료한다. 프로세스 자체가 죽거나 run 최종화 뒤 워커가 늦게 확정한 내용의 화면 동기화는 보장하지 않으므로 영수증으로 상태를 확인해야 한다.
- 한 정책 성공 후 다음 정책/플랫폼/출력 확인 실패도 이전 성공 기록을 보존한다. 응답 유실의 unknown은 미변경을 뜻하지 않는다. 결과 화면은 이전·새 버전과 확인 필요 상태를 구분한다.
- foundation을 같은 제품 프로세스 안에서 직렬화한다. AWS에는 expected VersionId를 받는 조건부 쓰기가 없으므로 외부 프로세스와의 조회→쓰기 경쟁을 완전히 방지할 수 없다. 계정 공용 경계에 프로젝트별 로그 Resource가 들어가는 기존 구조도 유지한다.

### 프롬프트 출처와 범위

**프롬프트 변경은 정준우 구현(서윤 cloud 브랜치 c4af865 출처).** `origin/main=38d718c31e6f82bdd7d4bdc8c02334590e5a5d78`, `origin/cloud=c4af8656c2a6b574311f37fe3f12d0272c99e66d`를 읽기 비교했다. 원격 fetch나 브랜치 변경은 하지 않았다. 비교 대상 repair_prompt.md에는 차이가 없었다.

| 항목 | 출처 / 이 작업 |
|---|---|
| ALB·앱·DB 최소 egress | c4af865 prompt.md에서 이식 |
| TG deregistration_delay=30 | c4af865 prompt.md에서 이식 |
| HTTPS TLS13-1-2-2021-06 | c4af865 prompt.md에서 이식 |
| 빈 app_database_url 시크릿 | c4af865 prompt.md에서 이식; logic.py의 DATABASE_URL ARN 출력 참조 해소 |
| health interval=5, timeout=3, healthy=2, unhealthy=2, /health/ready, 200 | 정준우 구현. c4af865는 interval·healthy를 명시하지 않았음 |
| /aws/ecs/프로젝트 로그 그룹 | 기존 프롬프트 유지, a16bbb4의 로그 권한 경계와 일치 |

서윤은 PR #25 헬스만 맡는다. rolling / TG 1개 대표 fixture의 정적 HCL·plan 검사 및 출력 참조를 고정하고, drain 300 또는 interval 30을 거부한다. 블루그린 휴면 검사와 역할은 보존한다. fixture 통과를 실제 생성 AI 호출·Terraform 공급자 검증·AWS 리허설 통과로 표현하지 않는다.


- 프롬프트 버전: `infra-aws-v3-rolling`. 기준본은 `infra-baselines/<project>/<PROMPT_VERSION>`에서만 읽는다. 버전만 바꾸고 무버전 캐시를 계속 쓰면 AI 호출을 건너뛰어 예전 HCL이 재사용되므로 디렉터리도 분리했다. 무버전/v2 기준본은 자동 재사용·복사하지 않는다. source=cache 및 source=fixture 구분을 테스트하며, 기존 기준본이 있던 환경은 새 버전 생성 경로로 들어간다는 동작 차이가 있다.
- 대표 fixture와 foundation을 함께 요약하면 전체 정책과 diff의 중복으로 8KiB를 넘었다. 생성/갱신 정책의 중복 전체 뷰만 제거하고 Statement/Resource diff를 유지했다. 변경 없는 정책의 뷰와 기존 8KiB 상한은 유지한다. 결합 요약을 실제 encode_meta로 검증하는 회귀도 추가했다. 임의로 큰 외부 정책 diff는 여전히 승인 전에 명확히 거부한다.

### 재개 작업의 최종 검증 — 이번 실행 결과

모든 make 명령에 `UV_NO_SYNC=1 PYTHONPATH=<cloud-staged>/src PYTHONDONTWRITEBYTECODE=1`을 지정했다. 공유 .venv 링크·의존성을 변경하지 않았다.

| 검사 | 결과 |
|---|---|
| `make -C harness test ARGS='tests/unit/cloud/infra tests/unit/test_boundary_execution.py tests/unit/test_boundary_views.py tests/unit/test_web.py tests/unit/test_ui_redesign.py tests/unit/test_ui_integration_fix10.py tests/unit/test_ui_settings_fix10.py tests/unit/test_executor.py tests/unit/test_executor_safety.py tests/unit/test_executor_diagnose.py tests/unit/test_deployment_service.py tests/unit/test_approval_meta.py tests/unit/test_approval_contracts.py --tb=short -q'` | **1096 passed, 0 failed, 10.57초** |
| `make -C harness lint type boundary contracts` | PASS. pyright 0 errors/warnings, import 경계 7 kept, 추가 계약 테스트 **48 passed**, 스키마 일치 |
| 경계 Stubber 집중 검사 | **58 passed**. 같음/없음/새 기본 버전/5개/승인 뒤 drift/다른 ARN, URL 인코딩 및 SDK dict, 전체 페이지·불완전 응답·부분 실패 포함 |
| 대표 rolling 생성·출력·속도·기준본 검사 | **13 passed**. HCL/plan drain 300·interval 30 거부 및 DATABASE_URL 출력 참조 포함 |
| `gitleaks git` (읽기 금지 경로 제외) | exit 0, **188 commits / 7.17 MB**, 발견 없음 |
| 변경·신규 파일 스캔 | `gitleaks dir --redact --no-banner <29개 파일 사본>` exit 0, 발견 없음 (`gitleaks-files.log`) |
| 최종 Git 확인 | `git diff --check` PASS, staged 변경 없음, HEAD a16bbb4 유지, .venv → ../../.venv 유지 |
| 전체 CI | **이번에는 미실행. 정준우가 샌드박스 밖에서 실행**. 위 과거 2599 passed·소켓 오류 기록은 이번 변경의 검증 결과가 아님 |

- 관련 검사에서 소켓 제한 실패는 없었다. 최종 통과 전에 발견한 정책 JSON percent 중복 디코딩·SDK dict 테스트 가정·승인 메타 중복 크기 문제는 수정하고 재검사했다. 초기 실패를 소켓 문제로 분류하지 않았다.
- 경계 갱신 독립 사후 검토의 두 지적(SDK 재시도, 취소/타임아웃 receipt 누락)을 수정했다. 설정 검사와 취소·타임아웃의 실제 서비스 run 영속화 회귀를 추가한 뒤 재검토 PASS를 받았다. 검토자는 읽기만 수행했고 테스트 수는 부모 직접 실행 결과다.
- 로그: `harness/var/validation/cloud-boundary-prompt-20261003/related-tests.log`, `static-checks.log`, `gitleaks-git.log`. 검증 산출물은 추적하지 않는다.
- 실제 AWS·Terraform·VM·Docker·claude 호출, git 쓰기·설정 변경, 금지 경로 읽기, 다른 worktree 수정은 하지 않았다. 이번 결과는 수정 코드와 로컬 모의 검사 완료이며 실배포·전체 CI·3분 리허설 완료가 아니다.


### 프롬프트 검토 범위

독립 검토는 요청한 프롬프트 항목·버전별 기준본 분리를 확인했다. 함께 지적한 egress 전체 강제와 정확한 health/TLS 값의 게이트 강제는 이번에 추가한 보안 판정이 아니다. 정준우 요청대로 기존 drain ≤30 / interval ≤10 / healthy ≤3 범위를 유지하며, 15초/10초 같은 허용 범위 값을 거부하도록 바꾸지 않는다. egress·TLS·health의 정확한 권장 값은 프롬프트 및 대표 fixture의 정합성 검사이고, 모든 AI 생성물에 해당 값을 강제하는 새 게이트를 구현했다는 뜻이 아니다. 기존 정적 정책 검사와 실제 제품 Checkov 경로도 대체하지 않는다.

### 이번 변경 파일 (첫 변경 줄)

| 파일:줄 | 상태 |
|---|---|
| `harness/contracts/schemas/apply_infra.output.json:3` | 수정 |
| `harness/docs/ai-usage/O1.md:704` | 수정 |
| `harness/docs/decisions/2026-10-03-cloud-bluegreen.md:48` | 수정 |
| `harness/docs/guides/O1-cloud-staged-gate-handoff-2026-10-03.md:136` | 수정 |
| `harness/fixtures/infra/rolling_v3/application.tf:1` | 신규 |
| `harness/fixtures/infra/rolling_v3/metadata.json:1` | 신규 |
| `harness/fixtures/infra/rolling_v3/network.tf:1` | 신규 |
| `harness/fixtures/infra/rolling_v3/pipeline.tf:1` | 신규 |
| `harness/fixtures/infra/rolling_v3/plan.json:1` | 신규 |
| `harness/tests/unit/cloud/infra/test_bluegreen_foundation.py:92` | 수정 |
| `harness/tests/unit/cloud/infra/test_bootstrap_pipeline.py:11` | 수정 |
| `harness/tests/unit/cloud/infra/test_boundary_versions.py:1` | 신규 |
| `harness/tests/unit/cloud/infra/test_generate_infra_rolling.py:1` | 신규 |
| `harness/tests/unit/cloud/infra/test_runtime.py:15` | 수정 |
| `harness/tests/unit/test_boundary_execution.py:1` | 신규 |
| `harness/tests/unit/test_boundary_views.py:1` | 신규 |
| `src/ddak/cloud/infra/boundary_versions.py:1` | 신규 |
| `src/ddak/cloud/infra/fixture.py:53` | 수정 |
| `src/ddak/cloud/infra/foundation.py:1` | 수정 |
| `src/ddak/cloud/infra/runtime.py:16` | 수정 |
| `src/ddak/cloud/infra/tools/generate_infra/logic.py:22` | 수정 |
| `src/ddak/cloud/infra/tools/generate_infra/prompt.md:45` | 수정 |
| `src/ddak/core/contracts/errors.py:14` | 수정 |
| `src/ddak/core/contracts/infra_evidence.py:1` | 신규 |
| `src/ddak/core/contracts/tools/apply_infra.py:7` | 수정 |
| `src/ddak/core/runtime.py:10` | 수정 |
| `src/ddak/executor/engine.py:29` | 수정 |
| `src/ddak/web/templates/approval.html:21` | 수정 |
| `src/ddak/web/templates/result.html:9` | 수정 |
