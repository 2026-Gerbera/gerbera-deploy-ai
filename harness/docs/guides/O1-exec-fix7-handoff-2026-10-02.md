# 후속 수정 7 인계 — 2026-10-02

기준 `e82d11a`(origin/main `a6bb097`, PR #12 포함), `o1/exec-fix6` clean에서 시작했다. 작업 사본만 수정하며 commit/push/PR, AWS·VM 접속, Terraform 직접 실행, 프로세스 종료 명령은 하지 않는다. [승인된 계약 기록](../decisions/2026-10-02-exec-fix7.md).

## 항목별 결과

| 항목 | 결과 | 내용 |
|---|---|---|
| 0 merge 기준 | DONE | 수정 전 전체 CI 양쪽 PASS: gitleaks 있음 1394 passed / 1 skipped / 4 deselected, 91.000초; 없음 1392 passed / 3 skipped / 4 deselected, 92.630초 |
| 1 출력 키 | DONE | 서비스·타깃 그룹·앱 SG 허용, cluster/subnet 유지, app 조립에서 서울 region 주입. FAKE 번들도 같은 출력 규약 사용 |
| 2 app 출력 보존 | DONE | 서비스·SQLite의 저장/조회 경로를 두 층 허용 출력으로 변경. run1 app apply → 컨트롤러 재시작 → run2 apply 없음에서 역할 ARN·secret_ids 유지 |
| 3 ECS 정책 | DONE | task 이름 web/was, service ignore_changes 두 필드와 circuit breaker 필수. HCL 및 plan 값 검사 |
| 4 이미지 저장소 | DONE(O1) | 공통 정규식으로 C1 선언·출력 검사 통일. C2 소비자 변경은 아래 요청 |
| 5 이월 관측 | DONE(O1 FAKE) | CarriedImageSource 계약 및 고정 조회 경로. v1 전체 → v2 WAS만 빌드에서 cloud web 관측 유지. 레거시 장부·승인 대기 재시작·잘못된 산출물 거부 확인 |
| 6 담당자 요청 | DONE(기록) | 아래 목록. 타 담당 구현 파일은 수정하지 않음 |

FAKE 번들은 SDK/CLI 경계와 리소스 값을 fixture로 대체한다. 일반 승인·plan 해시·잠금·출력 검사 경로를 사용하지만 실제 Terraform 구성 완결성이나 AWS 동작을 검증한 것은 아니다. **현재 C2 컨테이너 이름 소비자와 C3 health 키는 아직 기존 형식이므로, 실제 클라우드 E2E 완료로 보고하면 안 된다.**

## 담당자 요청 목록

### 승환

1. `cloud/deploy/_platform.py`: `cluster_name`, `ecs_service_name`, `public_subnet_ids`, `app_security_group_id` 사용. `ecs_container_name_<TIER>`를 없애고 task definition에 tier와 같은 이름의 컨테이너가 있는지 확인한다. `platform.get('region') or 'ap-northeast-2'` 기본값 유지.
2. 매 run의 `RELEASE_ID=ctx.run_id`를 태스크에 주입한다.
3. 마이그레이션 전용 태스크·계정을 연결한다. dbinit과 일반 migrate 구분은 아래 NEEDS_CONTEXT.
4. 새 시크릿 ARN을 태스크의 `valueFrom`에 연결한다. O1은 `app_secret_arn_<KEY>`와 `task_execution_role_arn`, `task_role_arn`, `dbinit_execution_role_arn`을 다음 run까지 보존한다.
5. `sync_env_to_cloud`를 등록하고 비밀이 아닌 키는 Secrets Manager에 보내지 않는다. dispatch 위치는 공동 결정 필요.
6. `codebuild.docker_hub_repo`는 `core.contracts.infra_outputs.IMAGE_REPOSITORY_PATTERN`을 fullmatch로 사용한다. 승인된 `namespace/repository` 패턴이며 태그·registry hostname·여분 경로는 받지 않는다.
7. `cloud/deploy/entry.py`의 `deploy_service`/`_observe`에 이월 tier fallback을 연결한다. 이번 빌드에 tier가 있으면 그것을 먼저 사용하고, 없으면 아래 경로를 읽는다. 기존 산출물을 새 `ReleaseArtifacts.snapshot`에 넣지 않는다.

```python
from ddak.core.contracts.release import CarriedImageSource

artifact = ctx.release_artifacts.images.get(tier) if ctx.release_artifacts else None
if artifact is None:
    origin = CarriedImageSource.model_validate(
        ctx.previous_release["cloud"]["image_sources"][tier]
    )
    artifact = origin.artifact
# artifact.ref == ctx.images[tier] 확인 후 실제 ECS 관측 digest를
# artifact.platform_digests[관측 platform]과 대조한다. 누락/불일치는 실패다.
```

항목은 `{artifact: {ref, index_digest, platform_digests}, release_id, source_sha, candidate_sha, snapshot, observation, carried_forward: true}` 형태다. `artifact`는 필수이고 기타 출처·이전 관측은 레거시 호환을 위해 null일 수 있다. O1이 prepare에서 artifact와 환경의 ref 일치를 검증하고 실행 컨텍스트에 위 구조를 제공한다. FAKE 회귀에서 index와 플랫폼 digest를 다르게 설정해 혼동을 검사했다.

### 서윤

- `cloud/health`의 health·TLS 소비 키를 `cluster_name`, `ecs_service_name`, `target_group_arn`으로 변경한다.
- region은 `platform.get('region') or 'ap-northeast-2'`로 읽는다. region Terraform 출력은 없다.
- digest 비교도 위 이월 tier artifact를 반영한다. 이번 build 이미지 목록만으로 전체 서비스 digest를 판정하면 안 된다.

### 준석

- `generate_infra` 출력 이름을 위 통일안에 맞춘다. 컨테이너 이름 출력과 region 출력을 생성하지 않는다. service HCL에 ignore_changes 및 circuit breaker를 포함한다.
- cloud 계획에서는 db tier의 build·deploy를 제외한다(RDS 사용). 온프렘 db tier와 섞지 않는다.

### 정준우·승환 NEEDS_CONTEXT

- 클라우드 dbinit과 마이그레이션 구분, 전용 태스크·계정의 연결 계약.
- `sync_env_to_cloud` dispatch 위치. 이번 O1 수정에서 임의 구현하지 않았다.

## 변경 파일

- 제품: `src/ddak/app.py`, `core/contracts/{infra_outputs,release}.py`, `core/store.py`, `executor/{images,service}.py`, `cloud/infra/{fixture,policy,plan,runtime}.py` (위 상대 경로의 공통 접두사는 `src/ddak/`).
- 계약 출력: `harness/scripts/export_schemas.py`, `harness/contracts/schemas/carried_image_source.json`.
- 테스트: `harness/tests/unit/cloud/infra/test_exec7_policy.py`, `harness/tests/unit/{test_followup7_outputs,test_followup6_images,test_followup5_service,test_infra_preparation,test_app_preparation}.py`.
- 문서: 이 인계서, `harness/docs/decisions/2026-10-02-exec-fix7.md`, `harness/docs/ai-usage/O1.md`.

## 검증

명령·UTC 시작·소요 시간·종료 코드·로그는 `harness/var/validation/followup7-20261002/`에 저장한다. 시간은 CI 벤치마크이며 실제 배포 시간이 아니다. 최초 핵심 경로 203개 통과, 첫 전체 CI에서 O1 회귀 4건을 확인했다. 이월 없는 환경의 기존 롤백 기준 보존과 region 기대값을 교정하고, null 형식의 레거시 출처 회귀도 추가했다. 보강된 집중 검증은 272개 통과(3.305초)다. 전체 최종 결과와 독립 검토는 아래 마감 기록에 남긴다. `make -C harness contracts-update`로 CarriedImageSource 스키마를 추가했다.

## 제안 커밋

1. 코드·테스트·생성 스키마: `fix: align cloud output contracts and preserve carried image observations`
2. 문서: `docs: record executor fix 7 contracts and integration handoff`

두 커밋의 중간 상태 CI는 별도로 실행하지 않았다. 실제 커밋·push는 정준우가 한다.


## 후속 7 최종 검증 마감 — DONE

- 최종 `make -C harness ci`(gitleaks 설치): **1442 passed / 1 skipped / 4 deselected**, 전체 **93.352초**, pytest 90.56초, exit 0 (`revised-with-gitleaks.log/json`).
- gitleaks 없는 PATH에서 동일 전체 CI: **1440 passed / 3 skipped / 4 deselected**, 전체 **94.954초**, pytest 92.75초, exit 0 (`revised-without-gitleaks.log/json`). 실제 scanner 테스트 2개만 추가 skip하며 제품 fail-closed는 그대로다.
- lint/type/import boundary/contracts 모두 PASS, `git diff --check` PASS. CI 90초 목표는 각각 약 3.4초·5.0초 초과했다. 실행 명령·UTC 시각·소요 시간·종료 코드는 `harness/var/validation/followup7-20261002/summary.json`과 개별 로그에 남겼다. 실패한 첫 전체 CI 로그도 보존했다.
- 독립 검토: Pre **REVISED**(Ampere), Post **PASS**(다른 검토자 Bohr), External Claude **NOT_REQUIRED**, O1 한정 Overall **PASS**. 검토자는 마지막 이월 조건·null 보강과 272개 집중 로그를 확인했다. 검토 당시 전체 CI는 진행 중이었고 위 최종 결과는 주 실행 세션에서 별도로 회수했다. 미해결 차단 발견사항 없음.
- 21개 변경 파일은 `harness/var/validation/followup7-20261002/changed-files.json` 및 인계서 목록에 기록했다. 금지된 다른 담당 구현 경로 변경 없음. commit/push/PR, AWS/VM 접속, 실제 Terraform 및 프로세스 종료 명령 없음.
- 남은 것은 인계서의 C2/C3/O2 연결 요청과 정준우·승환의 두 NEEDS_CONTEXT다. 실제 클라우드 E2E·전체 릴리스 완료를 의미하지 않는다.
