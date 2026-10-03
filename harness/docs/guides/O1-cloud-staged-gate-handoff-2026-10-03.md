# 대회 rolling 게이트와 배포 결정 정정 인계 — 2026-10-03

상태: **DONE**. 정준우의 10/3 14:35 결정에 따른 rolling 게이트·문서 정정·로컬 검증을 완료했다. 실제 배포 리허설은 별도다.

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
- providers·foundation·ECS 배포 코드·서윤님 prompt.md·공유 계약·의존성 파일은 변경하지 않았다.

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

## 검증

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

## 남은 확인

실제 rolling 리허설·배포 완료·3분 성능은 이번 로컬 검사로 검증하지 않았다. 프롬프트의 헬스 수치 보완과 실제 클라우드 리허설은 후속이다. 블루그린 작업은 대회 뒤까지 중단한다.
