# 클라우드 선별 통합 인계 — 2026-10-03

상태: **DONE** — 10/3 후속 지시 범위의 구현·검증·기록을 완료했다. 실제 AWS 배포와 새 main 재통합은 별도다.

## 위치와 기준

- 작업 트리: `.worktrees/cloud-int`, 브랜치: `o1/cloud-integration`.
- 시작 기준과 현재 HEAD: `971c8e64e0d2ba665663183287c9d116214b4c42`. 후속 검사 중 확인한 origin/main은 `b1a2434e571854aee14742ed3a307a015f56e00f`로 4커밋 앞서 있다. 기존 작업 트리를 유지했으며 새 main과의 재통합·충돌 검증은 이번 검사에 포함되지 않는다.
- 수입 기준: `origin/cloud` = `f401035c7013fa2b9218e1baad070fdcfe2498f4`. merge/rebase 없이 지정 경로의 내용만 선별했다.
- `.venv`는 `../../.venv` 심볼릭 링크이고 Git에 추가하지 않았다. 실행 시 `UV_NO_SYNC=1`과 이 작업 트리의 절대 `PYTHONPATH`를 지정했다.
- 원 작업 트리와 수정 11·12는 수정하지 않았다. 커밋·push·PR·태그·Git 설정 변경은 하지 않았다.

## 구현한 범위

- A1 생성기와 부분 교정 테스트를 선별 통합했다. 전역 AI 제한 20초는 유지하고 이 툴의 초안·교정 호출만 240초로 지정했다. main gateway와 맞지 않는 `data_max_len` 인자는 제거했고, 정제 후 기존 gateway 한도 4,096자를 넘으면 잘라 보내지 않고 명시적으로 거부한다.
- A2 조립·runtime·plan·정책을 통합했다. `GetSessionToken` 재발급 제거와 선택적 세션 토큰을 받았고, main의 이미지 저장소 검증과 ECS 소유권 검사를 보존했다. PLAN_ADDRESS는 누락된 name을 검증한 address에서 유도한다.
- ECS BLUE_GREEN, bake 0~2분, ECS controller, advanced configuration 및 고정 역할 ARN, hook 금지와 회로 차단기를 검사한다. listener rule의 서로 다른 두 TG·한 TG만 양수인 가중치·action 소유권과 TG 해제·헬스 제한을 검사한다. 테스트 리스너와 미확정 가중치도 거부한다. HCL과 plan after 양쪽을 검증한다.
- foundation SDK가 고정 ECS 인프라 역할을 생성하고 AWS 관리형 ELB 정책을 붙인다. 기존 역할은 경로·ARN·신뢰 정책·권한 경계와 모든 페이지의 부착/인라인 정책을 검사한 뒤 재사용한다. 승인된 관리형 정책 외 부착 정책과 인라인 정책이 있으면 쓰기 전에 거부한다. 충돌 생성은 재조회하고, 실패 시 부분 실행 표식을 보존한다. 역할 정의는 foundation 승인 해시와 승인 요약에 포함된다.
- 기존 권한 경계를 자동 버전 교체하지 않는다. SDK와 참고 HCL의 로그 범위는 `/aws/ecs/ddak-*`이다. 기존 경계와 다르면 승인 외 교체 없이 중단한다.
- A3 플랫폼 `app_secret_arn_<KEY>` 출력 허용과 generate_infra 600초·prepare_db 300초를 적용했다. tool_catalog는 생성기로 갱신했고 다른 공개 계약은 바꾸지 않았다.
- 인프라 적용 오류 상세는 기존 redact를 거친다. 금지 파일의 error.json 기록은 main의 고정 메시지 그대로다.

## 10/3 후속 결정 반영

1. **caller별 IAM 정책 공급:** 시연은 현행 자격증명을 사용한다. AdministratorAccess 보유는 사용자 확인에 따른 전제이며 실제 AWS 권한 조회는 하지 않았다. caller 정책 자동 부착은 구현하지 않는다. foundation/apply/deploy/ddak-readonly 권한 표를 결정 기록의 **대회 뒤 최소 권한 자동 공급** 항목에 남겼다.
2. **제품 preflight 부재:** core/registry.py의 preflight_check는 카탈로그 항목만 있고 src/ddak/ops/tools/에는 구현이 없다. app.py는 ops.tools를 탐색하지만 tool.py가 없으면 등록하지 않는다. 로컬 빌드 preflight와 harness/scripts/o1_demo.py의 운영 검사는 별개다. 제안 위치는 **src/ddak/ops/tools/preflight_check/tool.py의 cloud 분기**다. 이곳에서 ddak-readonly의 ecs:DescribeServices·elasticloadbalancing:DescribeRules·elasticloadbalancing:DescribeTargetHealth 권한 거부를 각각 식별 가능한 실패로 변환하는 방식을 제안한다. 지시대로 새 제품 경로나 수동 단계를 만들지 않았다.
3. **신규 RDS bootstrap:** 준비된 platform bootstrap에 한해 aws_iam_role.dbinit_execution 주소·ddak-<project>-dbinit-exec 이름·앱 경계/경로를 확인하고 rds!db-*의 GetSecretValue/DescribeSecret를 허용한다. HCL 직접 참조와 plan의 확정/미확정 역할 연결을 검사하며, 다른 역할·접두사·액션·계정·리전과 app/update, bootstrap 미준비를 거부한다. 변경 없는 와일드카드 정책도 검사한다. 기존 정확 ARN 허용은 보존했다.
4. **권한 경계·승인:** DescribeSecret는 rds!db-*와 dbinit PrincipalArn 조건의 별도 경계 문장으로 추가했다. 예외 정의와 실제 경계는 foundation 해시와 결합 infra 해시에 포함한다. 화면에는 범위·리전·가린 계정·역할 주소·액션이 보이며, 승인 템플릿 렌더링까지 검사한다. 개별 RDS ARN은 계속 해시로 숨긴다. 기존 경계의 자동 버전 교체는 하지 않는다. 이전 템플릿의 경계가 이미 있으면 불일치로 중단하는 제약은 유지된다.
5. **대회 뒤 정확한 ARN으로 좁힘:** bootstrap 제한은 생성·승인 시점의 제한이다. 이미 생성된 정책이 자동 만료되거나 정확 ARN으로 자동 축소되지는 않는다. 축소는 대회 후 제품 작업이다.
6. **main 이관:** error.json 직접 redact(src/ddak/executor/service.py)와 4,096자를 넘는 큰 교정 입력(core/ai/** 연계)은 이번 후속 범위에서 제외하고 main 스레드 담당으로 기록한다. 해당 파일은 수정하지 않았다.
7. **공유 .venv:** 이전 연결 변경 이력은 아래에 보존한다. 이번 후속에서는 사용자 결정에 따라 연결을 건드리지 않았고, 모든 검사를 UV_NO_SYNC=1 및 cloud-int/src의 PYTHONPATH로 실행한다. 독립 검토자는 소스 읽기만 수행한다.

이 후속의 완료 범위는 위 결정의 코드·회귀·기록 반영이다. 전체 클라우드 자동 부트스트랩, C2/C3 연동이나 실제 3분 배포를 검증한 것은 아니다.

## 출처와 선별 내역

다음은 저장소 루트 기준 경로다. A1/A2는 f401035 시점 파일을 수입했고, 각 경로의 마지막 원 변경은 **3066e67c09a53465a62dc90c541c8aff473bb3ba**다.

| 구분 | 수입 경로 | 이번 조정 |
|---|---|---|
| C3 A1 | src/ddak/cloud/infra/tools/generate_infra/logic.py | main gateway 호환·툴 내부 timeout·길이 및 symlink 검사 |
| C3 A1 | src/ddak/cloud/infra/tools/generate_infra/prompt.md | 원본 내용 유지, 블루그린 정합은 C3 후속 |
| C3 A1 | src/ddak/cloud/infra/tools/generate_infra/repair_prompt.md | 원본 내용 유지 |
| C3 A1 | harness/tests/unit/cloud/infra/test_generate_infra.py | 실제 gateway와 가짜 provider의 교정·timeout 회귀 추가 |
| O1 A2 | src/ddak/cloud/infra/assembly.py | 원 경로 통합 |
| O1 A2 | src/ddak/cloud/infra/runtime.py | main 이미지 검증 보존·역할 승인 표시·오류 정제 |
| O1 A2 | src/ddak/cloud/infra/plan.py | main ECS 검사 보존·주소 복구·블루그린 plan 검사 |
| O1 A2 | src/ddak/cloud/infra/policy.py | label 시그니처와 main inspect_ecs 결합·블루그린 검사 |
| O1 A2 | harness/tests/unit/cloud/infra/test_runtime.py | 역할 fake·오류 정제·이름 규칙 회귀 조정 |

부분 수입: `infra_outputs.py`의 플랫폼 app-secret 허용(3066e67), `registry.py`의 두 timeout(최신 원 커밋 **9177f7f5c99ad17c8084ca9f0ed3bc61a0705d49**). `providers/aws.py`·`terraform/foundation/main.tf`의 로그 변경(3066e67)은 넓은 원 범위를 받지 않고 `/aws/ecs/ddak-*`로 제한했다. 최초 통합에서는 bootstrap RDS UUID wildcard를 보류했고, 후속 사용자 결정에 따라 코드 소유 rds!db-* 예외를 별도로 구현했다. foundation의 경계 자동 교체, gateway·config·redact·SDK 의존성·cd 도구 우회·실행기·웹 변경은 수입하지 않았다. 선택 항목인 candidate.py도 변경하지 않았다.

## 전체 변경 파일

- `harness/contracts/schemas/tool_catalog.json`
- `harness/docs/ai-usage/O1.md`
- `harness/docs/decisions/2026-10-03-cloud-bluegreen.md`
- `harness/docs/guides/O1-cloud-integration-handoff-2026-10-03.md`
- `harness/tests/unit/cloud/infra/test_assembly_credentials.py`
- `harness/tests/unit/cloud/infra/test_bluegreen_foundation.py`
- `harness/tests/unit/cloud/infra/test_bluegreen_policy.py`
- `harness/tests/unit/cloud/infra/test_bootstrap_dbinit_policy.py`
- `harness/tests/unit/cloud/infra/test_bootstrap_pipeline.py`
- `harness/tests/unit/cloud/infra/test_exec7_policy.py`
- `harness/tests/unit/cloud/infra/test_foundation_files.py`
- `harness/tests/unit/cloud/infra/test_generate_infra.py`
- `harness/tests/unit/cloud/infra/test_pipeline_policy.py`
- `harness/tests/unit/cloud/infra/test_runtime.py`
- `src/ddak/cloud/infra/assembly.py`
- `src/ddak/cloud/infra/fixture.py`
- `src/ddak/cloud/infra/foundation.py`
- `src/ddak/cloud/infra/plan.py`
- `src/ddak/cloud/infra/policy.py`
- `src/ddak/cloud/infra/providers/aws.py`
- `src/ddak/cloud/infra/runtime.py`
- `src/ddak/cloud/infra/terraform/foundation/main.tf`
- `src/ddak/cloud/infra/tools/generate_infra/logic.py`
- `src/ddak/cloud/infra/tools/generate_infra/prompt.md`
- `src/ddak/cloud/infra/tools/generate_infra/repair_prompt.md`
- `src/ddak/core/contracts/infra_outputs.py`
- `src/ddak/core/registry.py`

## 수정 11·12와 겹침

- 착수 전에 두 작업 트리의 `git status --short`를 조회했다.
- 수정 11: 실제 변경 경로 교집합은 `harness/docs/ai-usage/O1.md`. candidate.py는 후보 겹침이었지만 이번에는 수정하지 않았다.
- 수정 12: `src/ddak/core/registry.py`, `harness/contracts/schemas/tool_catalog.json`, `harness/docs/ai-usage/O1.md`.
- registry에서 수정 12는 소유자 메타정보, 이번 작업은 timeout 두 값만 바꾼다. 결합 후 카탈로그는 다시 생성해야 한다. O1 기록은 서로의 append를 모두 보존한다.

## C2·C3 후속 요청

- C2: ServiceDeployment 단계로 완료 판정, 같은 이미지·env·secret의 중복 update 방지, 이월 tier 관측, 앱/마이그레이션 DB 계정과 secret 분리, provider 내부 plain/secret 주입, 단일 마이그레이션 태스크, StopServiceDeployment 우선 롤백 및 중복 롤백 방지.
- C3: 현재 수입 프롬프트에는 rolling·TG 한 개가 남아 새 게이트와 맞지 않는다. TG 두 개, 운영 listener rule, action ignore_changes, BLUE_GREEN/bake 1, 고정 인프라 역할 ARN, app_database_url secret, 아래 TLS 정책과 최소 egress, `/aws/ecs/ddak-*` 로그 이름을 함께 맞춰야 한다. health는 가중치가 있는 운영 TG·신규 리비전·이월 digest를 읽도록 바꿔야 한다.
- TLS check는 `ELBSecurityPolicy-TLS13-1-2-2021-06`을 이미 허용한다. check의 다른 값은 `ELBSecurityPolicy-TLS13-1-2-Res-2021-06`, health의 다른 값은 `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09`로 집합이 다르다. 공통값 하나를 사용할 수 있으며 health 파일은 수정하지 않았다.
- C3 기록은 C3.md로 옮기는 요청이 남는다. 이 작업에서 O2.md나 C3 담당 파일을 대신 수정하지 않았다.
- 빌드 분리와 관리 웹 디자인은 별도 작업이다. 미결정 감시 주기·배포 대상 설정은 확정하지 않았다.

## 최초 통합 검사 이력

공통 명령 접두사(이 작업 트리에서 실행):

```sh
UV_NO_SYNC=1 PYTHONPATH=/Users/joonwoojung/Desktop/01_workspace/04_SoftBank_hackerton/.worktrees/cloud-int/src
```

| 실행 명령(위 환경변수 포함) | 결과 |
|---|---|
| `make -C harness test ARGS='tests/unit/cloud/infra -q'` (수정 전) | 158 passed |
| `make -C harness contracts-update` 및 `make -C harness contracts` | 51개 스냅샷 생성, 최종 변경은 timeout 두 값, drift 없음 |
| `make -C harness test ARGS='tests/unit/cloud/infra tests/unit/test_infra_preparation.py -q'` | 415 passed / 1.89초 |
| `make -C harness ci` (후속 결정 전 최종) | exit 0; lint/type/boundary/contracts/test 모두 PASS; 2327 passed, 1 skipped, 4 deselected / pytest 192.21초, CI 합계 194.8초 |
| `gitleaks git --redact --no-banner --log-opts='origin/main..HEAD' .` | exit 0, 신규 commit 0개·0 bytes. 미커밋 변경 검증 근거로 사용하지 않음 |
| `gitleaks git --pre-commit --redact --no-banner .` | exit 0, 현재 tracked diff에서 발견 없음 |
| `gitleaks dir --redact --no-banner <수정·신규 파일만 복사한 임시 디렉터리>` | exit 0, 수정·신규 26개 파일에서 발견 없음. .venv 및 민감 경로 제외 |
| `git diff --check`, 금지 경로 diff 검사 | PASS |

첫 전체 CI는 신규 테스트의 포맷/린트로 실패했다(당시 테스트 2167 passed). 해당 서식·import 정렬·raw regex 표기를 고쳐 2167 passed의 전체 CI를 통과한 뒤, 독립 post의 역할 정책·트래픽 가중치 지적을 보완하고 IAM 목록 완료 표시 누락도 거부하도록 보완한 뒤 마지막 전체 CI에서 위 2327 passed를 확인했다. 중간 plan 통합 중의 PLAN_ADDRESS 실패와 CodeBuild fixture 이름 실패도 로그에 보존했고 최종에는 해소됐다. 공유 venv의 실제 import 위치는 이 작업 트리의 `src/ddak/cloud/infra/runtime.py`로 확인했다.

로그: `harness/var/validation/cloud-int-20261003/`의 `baseline.log`, `component.log`, `ci-first.log`, `ci-before-post.log`, `ci-before-pagination.log`, `ci.log`, `gitleaks-*.log`, `changed-files.json`. 검증 산출물은 추적하지 않는다. 실제 AWS·Terraform·Docker·Claude 호출은 없었다. 전체 CI의 약 195초는 로컬 테스트 시간이며 3분 배포 측정값이 아니다.

독립 검토: 사전 검토 REVISED. 첫 post는 전체 요구 미완과 기존 역할의 추가 정책 확인 누락·테스트 리스너/가중치 검사 누락을 지적했다. 두 신규 결함은 목록 페이지 확인과 HCL/plan 회귀로 수정했고, 새 검토자는 listener 보완을 PASS로 판정하고 IAM 목록의 IsTruncated 누락 처리를 추가 지적했다. 누락을 거부하도록 수정하고 재사용/동시 생성 회귀를 추가했다. 마지막 종료 조건과 그 회귀는 별도 검토자 PASS(4개 집중 회귀 통과)로 확인했다. listener 범위 PASS와 정책 목록 범위 PASS만을 뜻한다. 당시 전체 요구는 NEEDS_CONTEXT였다. 이후 사용자 결정으로 caller 정책 공급은 대회 뒤로 정리했고, RDS 예외는 이번 후속에서 구현했으며 error.json·큰 입력은 main으로 이관했다. 외부 Claude 검토는 사용자 명시 금지로 실행하지 않았다.

## 후속 결정 검증

- 회귀 파일: harness/tests/unit/cloud/infra/test_bootstrap_dbinit_policy.py. 허용 액션 두 개의 개별/동시 사용, 확정/unknown 역할, 다른 역할·접두사·리전·계정·액션, app/update·bootstrap 미준비, no-op, 승인 표시·렌더링·해시 무효화, SDK/HCL 경계 일치를 검사한다.
- 검사 환경: 모든 Python 검사에 UV_NO_SYNC=1 PYTHONPATH=/Users/joonwoojung/Desktop/01_workspace/04_SoftBank_hackerton/.worktrees/cloud-int/src를 지정했다. 공유 가상환경 연결을 변경하지 않았다.

| 실행 명령(위 환경변수 포함) | 최종 결과 |
|---|---|
| make -C harness test ARGS='tests/unit/cloud/infra -q' | exit 0, 616 passed / 2.71초. 이 중 후속 회귀 파일은 48개 |
| make -C harness ci | exit 0, lint/type/boundary/contracts/test 모두 PASS. 2375 passed, 1 skipped, 4 deselected / pytest 170.45초, 합계 177.0초 |
| gitleaks git --redact --no-banner --log-opts='origin/main..HEAD' . | exit 0, 신규 commit 0개·0 bytes, 발견 없음. 미커밋 변경 검증을 대신하지 않음 |
| gitleaks git --pre-commit --redact --no-banner . | exit 0, tracked 변경분 발견 없음 |
| gitleaks dir --redact --no-banner <수정·신규 파일 복사본> | exit 0, 수정·신규 27개 파일에서 발견 없음. .venv·민감 경로 제외 |
| git diff --check 및 수정 금지 경로 diff 확인 | PASS, 금지 경로·의존성 선언 변경 없음 |

- skip 1개는 별도 temp-box 프로젝트에서 실행하도록 정해진 앱 테스트다. 실제 AWS·Terraform·VM·Docker·claude를 호출하지 않았다. 위 시간은 로컬 CI 측정이며 시연 배포 시간은 아니다.

- 초기 집중 검사에서 승인 메타의 민감문구 정제 때문에 ARN 표시 8개가 실패했다(231 passed). 예외를 범위·리전·가린 계정을 명시하는 문구로 표시하여 해결했으며, 공용 정제기와 승인 메타 규칙은 변경하지 않았다. 초기 린트의 긴 줄 한 개도 수정했다.
- 독립 검토: 사전 검토의 역할 주소·DescribeSecret 단독 허용 범위·권한 경계·승인 마스킹 지적을 반영했다. 별도 사후 검토는 지정한 후속 코드 범위 PASS이며, 검토자는 소스만 읽었다. 테스트 실행 결과는 부모 실행의 위 수치와 구분한다. 외부 Claude는 사용자 금지로 호출하지 않았다.
- 로그: harness/var/validation/cloud-int-followup-20261003/. 검증 산출물은 추적하지 않는다.

## 공유 가상환경 변경 기록

최초 통합의 마지막 독립 검토가 `make -C harness test ARGS='tests/unit/cloud/infra/test_bluegreen_foundation.py::test_missing_truncation_flag_stops_reuse_and_creation_race'`를 **UV_NO_SYNC와 PYTHONPATH 없이** 실행했다. uv 출력에 `Building/Built ddak @ file://.../.worktrees/cloud-int`, `Uninstalled 1 package`, `Installed 1 package`가 있었다. 이는 읽기 전용 검토 범위 위반이다.

최초 통합 당시 주 세션이 확인한 상태: 공유 루트 `.venv/lib/python3.13/site-packages/ddak.pth`와 `ddak-0.0.0.dist-info/direct_url.json`은 `.worktrees/cloud-int/src` 및 해당 editable 프로젝트를 가리킨다. 해당 시각 이후 갱신된 dist-info는 ddak 하나였다. pyproject.toml·uv.lock 변경은 없다. 이전 editable 연결값은 착수 때 기록하지 않아 추정 복구하지 않았다. 10/3 후속 사용자 결정으로 연결 복구나 재변경을 하지 않는다. 검사 시 UV_NO_SYNC=1 및 작업별 PYTHONPATH를 유지한다.

주 세션의 CI는 전부 명시적인 `UV_NO_SYNC=1 PYTHONPATH=<cloud-int>/src`로 실행했고, 최종 CI도 exit 0이었다. 이 환경 변경을 검사 성공으로 정당화하지 않는다.

## 제안 커밋 단위

커밋은 사용자가 수행한다. 작업 트리 전체를 한 번에 stage하면 `.venv`가 포함될 수 있으므로 명시 경로만 선택한다.

1. C3 원 출처: `feat: Terraform 생성과 부분 교정 흐름 통합` — A1 네 경로, 출처 3066e67 명시.
2. O1 통합·정책·foundation: `feat: ECS 블루그린 인프라 게이트와 승인 역할 추가` — A2와 정책/SDK/fixture/회귀 테스트. 생성기 호환 조정은 O1 별도 hunk로 분리할 수 있다.
3. 공유 계약: `feat: 플랫폼 시크릿 출력과 인프라 툴 제한 시간 조정` — infra_outputs, registry, 생성된 tool_catalog.
4. O1 기록: `docs: 클라우드 블루그린 결정과 통합 인계 기록` — 결정 기록·이 문서·O1 append.

## 확인한 외부 기준

- [AWS ECS 인프라 역할과 PassRole](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AmazonECSInfrastructureRolePolicyForLoadBalancers.html)
- [ECS API별 IAM 권한 및 리소스](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ecs.html)
- [Terraform configuration JSON의 nested expression 직렬화](https://github.com/hashicorp/terraform/blob/main/internal/command/jsonconfig/expression.go)
