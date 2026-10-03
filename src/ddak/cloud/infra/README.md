# cloud/infra — 정준우 실행부 / 양서윤 generate_infra

> 10/3 정정: `tools/generate_infra` 실제 구현은 양서윤이고 PR #17로 통합됐다(이전 표기 "김준석"은 실제와 달랐다). 근거: [10/3 결정 따라잡기](../../../../harness/docs/decisions/2026-10-03-decisions-catch-up.md) 결정 17.

10/2 야간: 내부 API와 `validate_infra`·`plan_infra`·`apply_infra` 레지스트리 연결을 구현했다. 실제 AWS 리허설은 하지 않았다. 최신 범위는 [C1 가이드](../../../../harness/docs/guides/C1.md)를 따른다.

- `InfraRuntime.validate(files)`: 리소스 전용 HCL 정적 검사 → fmt → 자격증명 없는 init/validate → Checkov. 파일·provider 틀·lock 변경을 이후 단계에서도 검사한다.
- `plan(session, analyzer)`: 층별 S3 backend 초기화 → 저장 plan → Checkov/Access Analyzer → 8KiB 이하 C-18 요약. 개선 배포의 삭제·교체와 앱 밖 IAM 변경을 막는다.
- `apply(session)`: 주입된 잠금 검사와 승인 저장소에서 run/project/infra/plan SHA를 대조한다. 시도 표식을 먼저 남기고, 실패·중단 후 자동 재실행하지 않는다. 성공 후 원본 plan을 지우고 허용 출력만 반환한다.
- `refresh(session)`: output JSON의 이름·타입·sensitive를 검사한다. RunContext 변경은 실행기 연결부의 책임이다.
- `foundation_template`/`apply_foundation`: S3 state 버킷 설정과 앱·CodeBuild 각각의 권한 경계. 기반 템플릿 해시를 platform plan과 함께 infra 승인에 결합하며 별도 승인 클릭은 없다. STS 대상 계정을 확인하고, 기존 버킷은 관리 태그·계정, 경계는 내용이 일치해야 한다. 자동 삭제/기존 정책 교체 없음.

`AwsSettings.outputs`는 코드 호출자가 제공하는 출력 선언이다. 이름·타입은 `core/contracts/infra_outputs.py`의 플랫폼/앱별 허용목록으로 제한한다. 생성기가 쓸 리소스 주소와 필수 키 합의는 남아 있다. AI 입력에 세션 키·환경 출력·plan 원문을 넣지 않는다. `SessionKeys`는 실행 순간 메모리에서만 전달하며 repr에 값을 표시하지 않는다.

## 실행 환경

- Python 의존성: 사용자 승인으로 `python-hcl2>=7,<8` 추가.
- Terraform `>=1.11,<2`; 로컬 검증 1.15.8.
- AWS provider: `~>6.66`, `providers/aws.lock.hcl`에 6.67.0과 darwin_arm64/linux_amd64 체크섬 고정. 실행 디렉토리에서 `.terraform.lock.hcl`로 복사한다.
- Checkov 3.3.20 외부 CLI. `uv tool install checkov==3.3.20`. 프로젝트 Python과 분리한다. CodeBuild `environment`는 허용하지만 `environment_variable`은 비밀값 유입을 막기 위해 아직 받지 않는다. 없는 도구나 파싱 실패는 통과로 처리하지 않는다. 선택한 검사 중 해당 리소스에 적용되는 것이 없으면 검사 건수 0이며 안전성 증명이 아니다.
- fake 테스트: `make -C harness test ARGS='tests/unit/cloud/infra tests/unit/cloud/tls -q'`.
- 코딩 에이전트는 실제 apply/destroy를 실행하지 않는다. 오프라인 검증 fixture는 `harness/tests/fixtures/infra/app_v2.tf`.

## 코드 취합 때 확정할 연결

1. 생성기 입력/출력은 `core/contracts/tools/generate_infra.py`다. 조립부가 준 directory에 직접 하위 .tf만 쓰고 파일별 SHA-256·layer·출력 선언을 반환한다. 리소스만 AI, provider/backend/variable/output은 코드 소유다.
2. C-03 출력 이름/타입/필수 키: 기존 C3 PR의 ALB·ACM 이름과 C2 ECS·CodeBuild·시크릿 출력 연결. 임의 이름을 공통 스키마에 추가하지 않았다.
3. `ddak.app`이 생성 번들을 검증하고 `assembly.create_binding`으로 run별 runtime·SDK 단기 세션·Analyzer를 연결한다. 자격증명은 RunContext/DB에 저장하지 않는다. 생성기 미등록 시 이름을 포함해 실패한다. FAKE binding은 시험에서만 명시 주입한다.
4. 플랫폼·앱은 별도 실행/승인. 기존 저장소는 한 run당 infra 승인 1개다.
5. apply 뒤 출력 반영과 실패 상태 연결은 구현했다. 적용 시작 뒤 실패/출력 확인 실패는 NEEDS_HUMAN으로 잠금과 산출물을 보존한다. 앱 롤백으로 인프라 복구 완료를 기록하지 않는다. `apply-succeeded`는 출력 확인까지 성공해야 생긴다. last-applied bundle 운용/수동 복구 절차는 조립 시 확정한다.
6. CodeBuild 기본 소스는 `ai-prod` 후보 커밋 SHA(`sourceVersion` 고정), S3는 대체 경로로 확정됐다(10/1 사용자 결정). 현재 경계는 Docker Hub push 시크릿·전용 로그만 허용한다. S3 대체 경로 연결 시 전용 소스 버킷 읽기 권한을 추가한다. 생성되는 플랫폼 IAM은 현재 `ddak-codebuild`와 앱 경로 역할만 지원한다. 배포 역할·도메인 변수·전체 플랫폼 출력은 실제 생성 번들과 함께 확정/검증해야 한다. 앱 v2 fixture 통과로 전체 플랫폼 부트스트랩을 완료했다고 보지 않는다.
7. TLS 담당은 정준우다. ACM·443 리스너·HTTP→HTTPS 리다이렉트·HSTS는 플랫폼 Terraform이 만들고 `ensure_tls`는 확인만 한다(10/1 사용자 결정). 실제 생성 번들·출력 연결과 검증은 아직 남았다.

ALB 공개 HTTP 예외는 `AwsSettings.alb_security_group_addresses`에 코드가 지정한 SG의 `CKV_AWS_260`에만 적용하고 승인 headline에 표시한다. AI의 skip 주석은 거절한다. RDS 마스터 시크릿은 정확한 `ddak-<project>-dbinit-exec` 역할만 허용한다. 승인 대기와 분리해 validate/plan/apply 단계별 제한시간을 둔다.

추가 실행 경계: apply 기본 예산은 1200초, 출력 갱신은 별도 30초이며 조립 시 카탈로그/실행기 제한과 맞춰야 한다. 시간 초과 시 SIGINT를 한 번만 보내고 apply는 기본 120초(`CommandRunner.apply_stop_grace`), 다른 명령은 5초 기다린 후 SIGKILL로 회수한다. 종료 유예는 실행 예산에 추가된다. 결과는 불명확 상태로 남긴다. 같은 run/layer의 apply 시도 표식은 작업 사본 밖에 남아 새 객체도 재적용하지 못한다.

`root`는 모든 실행기 재시작/호출에 공유되는 영속 디렉토리여야 한다. foundation의 `marker`도 project/run별 고정 영속 경로여야 한다. 새 임시 경로를 매번 주입하면 재시도 차단이 유지되지 않으므로 실제 조립 시 이를 검증해야 한다. `ddak.app`은 service.root/infra를 공유 영속 root로 연결한다.

`close()`는 적용 전 또는 적용 성공이 확인된 작업 사본만 지운다. 불명확한 apply 산출물은 사람이 상태를 확인할 때까지 보존한다. SDK 클라이언트는 조립 코드가 짧은 connect/read timeout과 제한된 retries로 만들어 주입해야 한다. Analyzer ERROR는 차단하고 SECURITY_WARNING은 승인 요약에 표시한다(기존 요구사항).

DB 초기화 정책을 계획하려면 `AwsSettings.rds_master_secret_arn`에 플랫폼 출력의 정확한 ARN을 주입해야 한다. 이름 패턴만으로 전체 RDS 마스터 시크릿을 허용하지 않는다. C2가 주입할 컨테이너 환경변수는 AI Terraform에서 받지 않는다. `region`/시크릿 복제/리소스 정책, 프로젝트 범위를 벗어나는 시크릿 이름을 거부한다.

현재 HCL 파서가 내부 표현식을 펼치지 않는 heredoc은 전체 거절한다. 문자열은 일반 따옴표, IAM·컨테이너 JSON은 `jsonencode`로 생성해야 한다.

## 10/2 추가 파일과 연결

- `terraform/foundation/`: 제품이 직접 실행하지 않는 HCL 참고본. 실제 기반 생성은 infra 승인 뒤 `foundation.py` SDK 코드가 실행한다. state 버킷 + 경계 2개이며 deployer 역할은 AI 번들 밖이다.
- `bindings.py`: `bind_infra`/`unbind_infra`, validate/plan/apply 호출 경계. FAKE에는 fixture runner 필수.
- `executor/infra.py`: apply 출력의 기존 flat/nested cloud 설정 보존·병합. 온프렘 설정은 보존.
- 새 앱 시크릿 출력은 `app_secret_arn_<KEY>`, 기존 `secret_arn`은 호환만 유지한다.
- 생성기 계약과 O1 조립 경로는 구현했다. 실제 HCL 생산 툴 구현은 O2가 연결해야 한다. 테스트 HCL은 source=fixture이며 실제 AI 산출물로 표시하지 않는다.

## 후속 수정 5 (10/2)

- 플랫폼 `codebuild_project_name`·`image_repository` 등 허용 출력을 release에 보존해 다음 run의 `ctx.platform.cloud`에 재주입한다. FAKE/REAL을 구분하고 ARN 식별자는 값 마스킹 없이 검증 후 보존한다. 민감 Terraform output은 받지 않는다.
- `image_repository` 출력 선언은 JSON 문자열 리터럴(예: `"team/app"`) + `string`이다. 나머지 출력 선언은 허용된 AWS 리소스 속성 참조다.
- AI 번들의 `aws_s3_bucket*`는 코드 소유 state bucket을 수정할 수 없다. plan의 대상 bucket이 미확정이어도 승인을 거부한다.
- CodeBuild source.buildspec은 `OVERRIDE_REQUIRED_BUILDSPEC`의 실패 전용 인라인 값이어야 한다. C2는 StartBuild에 코드 소유 buildspecOverride를 전달해야 한다. 프로젝트 environment_variable 금지는 유지한다.
- `start_build_policy`는 외부 deployer 역할의 정책 설계다(이 함수는 역할을 생성/적용하지 않는다). override 이름은 BUILD_TIERS/RELEASE_ID/SOURCE_REVISION/IMAGE_REPO만, source.location은 승인 repo URL 또는 정확한 승인 S3 객체 경로만 허용한다. IAM의 buildspec 조건은 내용 검증이 아닌 존재 검사다. 내용은 코드 소유 C2 실행 경로가 고정해야 한다([AWS 조건 키](https://docs.aws.amazon.com/codebuild/latest/userguide/action-context-keys.html)).
- local backend 실패 대비는 프로젝트·층별 영속 `bootstrap-recovery-*.json`을 쓰는 방식이다. run/work_dir/local_state_path/plan 해시를 남기며 다른 run도 복구 확인 전 거부한다. remote 이전 성공 시에만 지운다. state 내용을 자동 출력·삭제하지 않는다.
- 버킷 생성 후 태그를 먼저 쓰고, 태그 실패에도 `*-foundation-bucket-created.json` 증거를 보존한다. 관리 태그 없는 기존 버킷을 자동 인수하지 않는다. 복구 시 기록/계정/버킷/state를 사람이 확인한 뒤 처리해야 한다.
- apply 1200초, state 이전 init 120초, 출력 갱신 30초는 각각 별도 예산이다. 카탈로그 총 제한 안에서 조립한다.
