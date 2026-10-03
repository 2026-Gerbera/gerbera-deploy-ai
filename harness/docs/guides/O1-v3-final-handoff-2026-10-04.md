# v3 리허설 직전 수정

기준: `.worktrees/s3-infra`, `o1/s3-infra`, `359fb42`.

## 패치 렌더러

- 함수 인자로 환경키를 받는 모듈 함수는 호출부의 리터럴 키를 수집해 충돌을 검사한다. 위치·키워드 인자와 리터럴 기본값을 처리한다.
- 계산된 키, 인자 재대입, 계산된 래퍼 호출 인자는 계속 거부한다. 기존 키와의 충돌 검사, 패치 검사기의 허용 범위는 유지한다.
- 실제 `app-v3`의 추적 파일 30개를 검증 사본으로 복사했다. 비밀 경로는 복사하지 않았으며 앱 원본은 수정하지 않았다. 결과는 `flaskr/uploads.py`의 아래 한 줄 교체뿐이고 `check_patch`를 통과했다.

  ```diff
  -IMG_DIR = "img"
  +IMG_DIR = os.environ['IMG_DIR']
  ```

- 기존 `fix11-demo-hardcoding.patch`는 문서의 원본 문맥을 가진 fixture에 그대로 적용한 뒤 secret_key·local_address 렌더와 검사 통과를 확인했다. 이 문서 패치의 옛 원본 문맥이 최신 앱의 `create_app` 문맥과 같다는 뜻은 아니다.
- 검증 사본은 제거했다. `harness/var/validation/v3-final/v3-render.diff`, `v3-render-result.json`만 남겼다.

## 운영 v3 버튼

- 공용 `ACTIONS`와 CLI에 `prepare-v3`를 추가했다. 태그 v3의 트리, 현재 prod 부모, `demo/v3-` 새 브랜치와 요청한 PR 제목·목적을 사용한다.
- v3 태그 조회·fetch는 필수 v1·v2 fetch와 분리했다. 태그가 없으면 v1·v2는 그대로 동작하고 v3 생성 요청만 `v3 태그 없음`으로 거부한다.
- `_demo_reset.html`은 새 폼 한 줄만 추가했다. 기존 `/ops/demo/{action}` 경로가 공용 액션을 검증하므로 라우트 코드는 바꿀 필요가 없었다.
- 상태 조회가 태그를 확인하기 전까지 버튼을 비활성화한다. 조회 후 `v3 시연 PR 준비` / `v3 태그 없음` / `v3 태그 확인 실패`로 표시한다. 상태 라벨과 `/version`의 v3도 표시한다.
- 기존 v1 복원 버튼을 재사용한다. 가짜 Git·GitHub에서 v3 PR과 v3→v1 복원 PR의 트리·부모, prod·태그 불변, force 없는 demo 브랜치 push를 확인했다.
- 기존 저장소 제거 승인 테스트에서 remove 의도와 삭제 4개가 `AWAITING_APPROVAL`에 표시됨을 확인했다. 실제 S3 삭제는 실행하지 않았다.

## 추가 CI 실패 3건

- env reset은 `SECRET_KEY`만 제거하고 공개값 `IMG_DIR=img`와 주석·다른 키를 보존하는 것이 맞다. 테스트를 전체 문자열 비교로 유지하면서 기대값에 해당 공개값을 추가했다.
- was 템플릿에서 기존 SQLite `/data` 볼륨을 첫 항목으로 유지하고 업로드 `/app/img`를 별도 두 번째 항목으로 뒀다. 테스트는 이름·경로·순서를 모두 검사한다.
- `patch_patterns.py`는 `_string` 결과를 지역 변수로 한 번 받아 `None`을 좁힌 뒤 문자열 포함 여부를 검사한다.

## 검증

- `UV_NO_SYNC=1 PYTHONPATH="$PWD/src" make -C harness fmt`: 통과.
- 관련 pytest: intents, patch_check, demo_cycle, demo_reset(backend·ops·JS 포함), storage_approval, onprem test_demo·test_onprem_cli — **268 passed / 10.83초**.
- Node VM 검사에서 window 없이 v3 버튼의 준비·없음·오류 상태 전환을 확인했다.
- `CI=true UV_NO_SYNC=1 PYTHONPATH="$PWD/src" make -C harness check`: exit 0. **4525 passed, 1 skipped, 4 deselected / pytest 345.35초, 전체 348.6초**. lint·type·boundary·contracts·test·attribution 모두 PASS. 건너뛴 1건은 별도 temp-box 프로젝트에서 실행하는 앱 테스트다.
- 전체 로그: `harness/var/validation/v3-final/check.log`.

## 변경 파일

- 제품: `plan/patch/intents.py`, `core/patch_patterns.py`, `core/demo_cycle.py`, `core/demo_backend.py`, `core/demo_reset.py`
- 웹: `web/templates/_demo_reset.html` 한 줄, `_demo_state.html`, `web/static/demo_reset.js`
- 스크립트: `harness/scripts/demo_cycle.py`, `o1_onprem.py`
- 테스트: `unit/plan/patch/test_intents.py`, `unit/test_demo_reset.py`, `unit/onprem/deploy/test_demo.py`, `test_onprem_cli.py`
- 이 인계서. 경로 생략된 제품·웹 파일은 `src/ddak/`, 테스트는 `harness/tests/` 기준이다.

## 남은 확인

- 원격 앱 저장소의 v3 태그가 있어야 버튼이 활성화된다. 이번 작업에서는 원격에 쓰거나 태그를 만들지 않았다.
- 앱 원본·기존 다른 worktree·활용 기록은 수정하지 않았다. 실제 배포·S3·복제본 업로드와 3분 시간은 운영자 리허설에서 확인해야 한다.
- 디자인 작업과 합칠 때 `_demo_reset.html`의 새 폼 ID 및 `_demo_state.html`의 `data-demo-v3` 표시, `demo_reset.js` 연결을 함께 유지한다.

## 추가 결정: UPDATE 빌드 병렬화

- 승인 전 계획 조립에서 UPDATE와 기존 빌드 입력을 확인한다. CodeBuild는 `codebuild_project_name`·`image_repository`, local은 `image_repository`가 준비된 경우 빌드의 `infra_ready` 대기만 제거한다.
- BOOTSTRAP의 인프라 선행 순서, cloud 배포의 순차 인프라 의존성, 이벤트·계획 모델은 유지한다. 실행 중 대기를 무시하지 않는다.
- 가짜 툴의 이벤트로 UPDATE 빌드 시작이 인프라 종료보다 앞서는지, BOOTSTRAP에서는 반대인지, 인프라 실패 뒤 빌드는 끝나도 cloud 배포는 실행되지 않는지 확인했다.
- 관련 52개 테스트 통과. 실환경 20~30초 단축이나 총 3분 충족은 아직 측정하지 않았다.

## 추가 결정: 읽기 쉬운 버킷 순번

- 이름은 `gerbera-<platform>-images-<n>`이다. 플랫폼은 소문자 1~31자, 번호는 1~999999다. 이름·검증·접두사 규칙은 `core/storage.py`가 소유한다.
- `<run_dir>/storage/bucket-sequence.sqlite3`에 프로젝트별 마지막 번호와 run별 예약을 원자적으로 보관한다. 번호를 먼저 소비한 뒤 계획 준비의 읽기 세션으로 HeadBucket을 호출한다. 404만 사용하고 200·403은 건너뛴다. 최대 10개를 시도하며 그 외 오류는 준비 실패로 표시한다.
- 같은 run 재준비는 예약한 이름을 유지한다. 거절·오류·삭제된 번호는 되돌리지 않는다. remove는 현재 cloud 출력의 이름을 쓰고 그 번호를 장부에 보존한다. 이 장부를 삭제하면 과거 삭제된 번호를 복원할 수 없으므로 컨트롤러 데이터와 함께 보존해야 한다.
- 생성기는 번호를 고르지 않는다. `storage.tf`는 `var.upload_bucket`을 읽고, 제품이 변수 선언과 `upload.auto.tfvars.json`을 제공한다. 변수 파일과 예약 이름도 승인 무결성 검사에 묶인다.
- 기준본 버전을 `infra-app-storage-v2`로 올렸다. 계정·플랫폼·역할이 같은 HCL은 번호가 달라도 재사용된다. 옛 리터럴 버킷 HCL 캐시는 재사용하지 않는다.
- 태스크 inline 정책은 예약한 버킷 하나와 그 객체만 허용한다. app 권한 경계는 `arn:aws:s3:::gerbera-<platform>-images-*` 및 `/*`이며 번호와 독립적이다. 이미 같은 경계라면 다음 번호에서 새 정책 버전을 만들지 않는다.
- 승인 데이터 `infra.storage.bucket`은 전체 이름을 표시한다. create/remove 정책은 실제 계획의 이름과 예약·현재 출력의 일치를 검사한다.
- 새 규칙 이전의 `ddak-...-uploads-<account>` 출력은 새 관문에서 거부한다. 기존 이름의 버킷이 실환경에 남아 있다면 별도 호환 이관이 필요하며, 이 작업에서 기존 자원을 자동 변경하거나 삭제하지 않았다.

## 추가 변경 파일

- 조립: `src/ddak/app.py`, `src/ddak/plan/flow.py`
- 순번: `src/ddak/core/storage.py`, `storage_sequence.py`, `src/ddak/cloud/infra/storage_allocation.py`
- 인프라: `assembly.py`, `bindings.py`, `runtime.py`, `policy.py`, `plan.py`, `storage_policy.py`, `storage_bundle.py`, `storage_fixture.py`, `providers/aws.py`, `tools/generate_infra/prompt_storage.md` (모두 `src/ddak/cloud/infra/` 기준)
- 회귀: `test_update_build_gate.py`, `test_storage_sequence.py`, `test_storage_approval.py`, `test_storage_v3.py`, `cloud/infra/test_generate_storage.py`, `test_storage_policy.py`, `test_storage_runtime.py` (모두 `harness/tests/unit/` 기준)

## 추가 결정: IMG_DIR 필수 패치의 결정성

- 클라우드를 포함한 대상에서 `scan_storage`의 `hardcoded_dir` 증거가 있으면 해당 위치를 필수로 다룬다. 누락 시 최대 2회의 기존 시도 안에서 위치만 피드백하고, 여전히 누락되면 고정된 `IMG_DIR` 의도를 코드로 채운다. 추가 외부 호출은 없다.
- `render_intents`·`check_patch`·엄격 비밀값 검사를 그대로 거친다. 등록 툴 출력의 `PatternName`에도 `local_storage_dir`를 추가했다. 이는 기존 패턴의 보안 검사 확대가 아니라 이미 지원하는 패턴의 출력 계약 누락 교정이다.
- 승인 메타와 파일별 제안 이유에 `규칙으로 보완`을 표시한다. 출처는 실제 응답 출처와 함께 보존한다. 선택 검토 화면에서도 필수 저장 경로 제안을 빼고 진행할 수 없다.
- 코드 수정 OFF이면 새 제안을 만들지 않는다. 기존 승인 원장으로 저장 경로 패치를 재사용할 수 있으면 계속하고, 그렇지 않으면 `클라우드 IMG_DIR 패치 필요, 코드수정 켜기`로 준비를 중단한다.
- 증거가 없는 v1·v2에는 아무것도 추가하지 않는다. 온프렘 단독·다른 패턴의 기존 동작은 유지한다. 복수 IMG_DIR 위치는 기존 렌더러의 환경키 충돌 검사로 차단되며 이번에 그 경계를 넓히지 않았다.
- 관련 `plan/patch` 테스트 **500 passed / 43.31초**. 동일 입력 바이트 일치, 누락 보완·후속 응답 완성, 승인 메타, 토글·원장 재사용을 확인했다.
- 추가 파일: `src/ddak/plan/patch/generate.py`, `pipeline.py`, `tool_review.py`, `src/ddak/core/contracts/tools/patch_config.py`, `harness/tests/unit/plan/patch/test_storage_required.py`, 갱신된 계약 스키마.

## 추가 검증 기록

- 순번·탐지·생성·캐시·승인 관련 **44 passed / 1.82초**, 예약 이름 표시·변조 차단 승인 테스트를 추가한 뒤 **3 passed / 0.87초**.
- 저장소 정책·runtime·기반 관련 **190 passed / 1.13초**. create 1 → remove 1 → create 2 동안 app 경계 정책 버전 생성은 1회였다.
- `make -C harness contracts-update`: 57개 스키마 생성. `local_storage_dir` 출력 계약을 동기화했다.
- `make -C harness fmt`: 통과. 처음 발견한 조회 예외 처리 lint 1건을 수정했다.
- 최종 전체 검사 로그는 `harness/var/validation/v3-final/check-additions.log`에 별도로 보존한다. 이전 `check.log`는 덮어쓰지 않는다.

## 제안 커밋 메시지

- `fix: v3 저장 경로 패치와 시연 PR 경로 안정화`
- `perf: 개선 배포 빌드와 인프라 적용 병렬화`
- `feat: 업로드 버킷 순번 예약과 승인 이름 고정`

커밋·push·PR·태그·전역 설정 변경은 수행하지 않았다. 별도 worktree와 활용 기록은 수정하지 않았다.

## 최종 전체 결과

- `CI=true UV_NO_SYNC=1 PYTHONPATH="$PWD/src" make -C harness check`: **exit 0, 4660 passed, 0 failed, 1 skipped, 4 deselected**. pytest **359.97초**, 전체 **363.3초**.
- lint·type·boundary·contracts·test·attribution 모두 PASS. `git diff --check`도 통과했다.
- `harness/var/validation/v3-final/check-additions.log`, `final-summary.json`, `changed-files.txt`에 결과와 파일 목록을 남겼다.
- 작업 시작 전에 있던 `.venv` 연결은 유지했고 변경 파일 목록에서 제외했다. 이번 테스트의 임시 디렉터리는 정리했다.

최종 변경 파일(저장소 상대 경로):

- `harness/contracts/schemas/patch_config.output.json`
- `harness/docs/guides/O1-v3-final-handoff-2026-10-04.md`
- `harness/scripts/demo_cycle.py`
- `harness/scripts/o1_onprem.py`
- `harness/tests/unit/cloud/infra/test_generate_storage.py`
- `harness/tests/unit/cloud/infra/test_storage_policy.py`
- `harness/tests/unit/cloud/infra/test_storage_runtime.py`
- `harness/tests/unit/onprem/deploy/test_demo.py`
- `harness/tests/unit/onprem/deploy/test_onprem_cli.py`
- `harness/tests/unit/plan/patch/test_intents.py`
- `harness/tests/unit/plan/patch/test_storage_required.py`
- `harness/tests/unit/test_demo_reset.py`
- `harness/tests/unit/test_storage_approval.py`
- `harness/tests/unit/test_storage_sequence.py`
- `harness/tests/unit/test_storage_v3.py`
- `harness/tests/unit/test_update_build_gate.py`
- `src/ddak/app.py`
- `src/ddak/cloud/infra/assembly.py`
- `src/ddak/cloud/infra/bindings.py`
- `src/ddak/cloud/infra/plan.py`
- `src/ddak/cloud/infra/policy.py`
- `src/ddak/cloud/infra/providers/aws.py`
- `src/ddak/cloud/infra/runtime.py`
- `src/ddak/cloud/infra/storage_allocation.py`
- `src/ddak/cloud/infra/storage_bundle.py`
- `src/ddak/cloud/infra/storage_fixture.py`
- `src/ddak/cloud/infra/storage_policy.py`
- `src/ddak/cloud/infra/tools/generate_infra/prompt_storage.md`
- `src/ddak/core/contracts/tools/patch_config.py`
- `src/ddak/core/demo_backend.py`
- `src/ddak/core/demo_cycle.py`
- `src/ddak/core/demo_reset.py`
- `src/ddak/core/patch_patterns.py`
- `src/ddak/core/storage.py`
- `src/ddak/core/storage_sequence.py`
- `src/ddak/plan/flow.py`
- `src/ddak/plan/patch/generate.py`
- `src/ddak/plan/patch/intents.py`
- `src/ddak/plan/patch/pipeline.py`
- `src/ddak/plan/patch/tool_review.py`
- `src/ddak/web/static/demo_reset.js`
- `src/ddak/web/templates/_demo_reset.html`
- `src/ddak/web/templates/_demo_state.html`
