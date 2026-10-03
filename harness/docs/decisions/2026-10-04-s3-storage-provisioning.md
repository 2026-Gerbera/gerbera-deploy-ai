# 2026-10-04 업로드 저장소 자동 프로비저닝

정준우 승인: 로컬 `IMG_DIR` 업로드 디렉토리를 클라우드 S3로 대응한다. 공통 사양 `.orchestrator/s3v3_spec.md`가 이전 `UPLOAD_BUCKET` 계획보다 우선한다. 작업 기준은 `o1/s3-infra`의 `5fb05fc`이며 로그 범위 PR #42는 이미 반영된 상태였다.

## 승인 항목

- 출력 키 `upload_bucket`을 app 출력 허용 목록에 추가한다. 생성 apply 후 기록하고 제거 apply 후 기존 nested/flat 출력에서 모두 뺀다.
- 관문 완화 3건: app 층 S3 세 타입 허용, 실재·경로·권한 경계를 확인한 `<platform>-task`에 `aws_iam_role_policy.uploads` 부착 허용, UPDATE/remove에서 고정 저장소 네 주소의 순수 delete만 허용. 다른 삭제·교체와 IAM 파괴 변경은 기존대로 거부한다.
- `ddak-app-boundary`에 해당 버킷 `/*`의 GetObject·PutObject와 버킷의 ListBucket만 추가한다. UPDATE의 같은 승인에 템플릿 해시·현재 버전/문서 해시·문장 차이를 묶고 승인 뒤 CreatePolicyVersion으로 기본 버전을 바꾼다. 실행 직전 변경 또는 버전 5개는 중단한다. 버전 자동 삭제는 하지 않는다.
- 이전 장부 13에서 제외했던 앱 S3를 이번 v3 시연 범위에서 재개한다. 태스크 역할을 비워 두던 방침도 위 세 S3 동작에 한해 해제한다.

## 연결 결과

저장소 생성은 app UPDATE의 `_infra_storage.intent=create`에서만 전용 프롬프트를 사용한다. 입력은 근거 file/line/kind·플랫폼·계정·태스크 역할 이름이다. 기존 플랫폼 `prompt.md`, `repair_prompt.md`, `PROMPT_VERSION`과 플랫폼 캐시는 유지한다. 제거는 생성 호출 없이 리소스가 없는 번들을 만든다.

`storage.tf`의 근거 헤더는 번들 해시와 계획에 묶인다. validate·plan을 통과한 LIVE create만 `infra-baselines/<platform>/infra-app-storage-v1/<입력 해시>/`에 저장한다. 계정·플랫폼·태스크 역할이 같은 다음 생성은 source=cache다. FAKE는 source=fixture로 표시하며 캐시를 저장하지 않는다.

승인 summary와 meta의 `storage`에는 intent·rationale·evidence·bucket·env(IMG_DIR)·files·source를 넣는다. 계정은 가리고 저장소가 있는 전체 승인 요약은 8KiB로 제한한다. 기존 플랫폼 요약의 16KiB 제한은 유지한다. 저장소 UPDATE는 기반 버킷이나 ECS 인프라 역할을 다시 생성하지 않는다. app 경계만 갱신하며 이전/새 버전을 기존 실행 기록 경로와 영속 영수증에 남긴다.

자동 배포 조립이 분석 결과의 `_infra_storage`를 덮어쓰지 않도록 보존한다. v3→v1 remove 입력에서 삭제 4개가 승인 데이터에 도달함을 fixture로 확인했다. `core/demo_reset.py`의 기존 승인 대기 경로를 그대로 쓰며 별도 수정하지 않았다.

## 변경 파일

- 생성: `src/ddak/cloud/infra/tools/generate_infra/logic.py`, `storage.py`, `prompt_storage.md`.
- 저장소 계약·캐시·관문: `src/ddak/cloud/infra/storage_bundle.py`, `storage_policy.py`, `policy.py`, `plan.py`, `providers/aws.py`.
- 승인·실행: `src/ddak/cloud/infra/assembly.py`, `bindings.py`, `boundary_versions.py`, `runtime.py`, `src/ddak/app.py`, `src/ddak/executor/approval_meta.py`, `infra.py`.
- 출력·공통 연결: `src/ddak/core/contracts/infra_outputs.py`, `src/ddak/core/storage.py`.
- FAKE: `src/ddak/cloud/infra/fixture.py`, `storage_fixture.py`.
- 테스트: `harness/tests/unit/cloud/infra/test_assembly.py`, `test_generate_storage.py`, `test_storage_policy.py`, `test_storage_runtime.py`, `harness/tests/unit/test_storage_approval.py`.
- 기록: 이 결정 문서.

## 검증

저장소 worktree에서:

```sh
UV_NO_SYNC=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src" make -C harness fmt
```

결과: 성공. 형식 변경 6개, 나머지 544개 유지, 정적 검사 통과.

`harness/`에서:

```sh
UV_NO_SYNC=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/../src" ../.venv/bin/python -m pytest tests/unit/cloud/infra tests/unit/test_storage_approval.py tests/unit/test_approval_meta.py tests/unit/test_infra_preparation.py tests/unit/test_followup7_outputs.py tests/unit/test_demo_reset.py -q
```

최종 결과: **1089 passed, 4.60초**. 생성 4+경계 갱신, 삭제 4, 다른 삭제·이름·권한 범위 거부, 실제 역할 메타 확인, 승인 뒤 경계 변경/버전 5개 중단, 캐시 재사용, 제거 승인과 출력 정리를 확인했다. 기존 테스트의 단순 mock 설정 누락 1개를 보완한 뒤 통과했다. `git diff --check` 통과. 전체 CI·gitleaks·실제 외부 서비스 및 Terraform 실행은 하지 않았다.

## 후속 작업 / NEEDS_CONTEXT

- 추가 결정은 없다. `src/ddak/core/storage.py`는 탐지 작업과 병합하기 위한 임시 계약 모듈이다. `scan_storage`는 미구현 오류로 멈춘다. 병합 때 탐지 작업의 실제 구현으로 교체하고 `_infra_storage` 공급·IMG_DIR 주입·스모크 및 별도 화면 구현을 함께 연결해야 한다.
- 실제 AWS 적용과 업로드 시연·3분 예산은 이번 검증에 포함되지 않았다. 사용자 리허설에서 확인한다.
- 최대 길이 플랫폼 이름은 고정 버킷 이름의 S3 길이 제한과 별도 대조가 필요하다. 이번 시연 플랫폼 기본 경로만 검증했다.
- 저장소 intent와 기존 app의 추가형 리소스가 섞이는 번들의 엄격한 전체 주소 제한은 후속 정리 대상으로 남긴다. 저장소 밖 삭제·교체 거부는 이번 범위에서 검사한다.
