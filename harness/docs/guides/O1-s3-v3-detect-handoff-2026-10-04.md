# v3 저장 경로 탐지·패치·주입·스모크 인계

기준: `o1/s3-detect`, `a5786e7`, 공통 사양 `.orchestrator/s3v3_spec.md`.
이 작업은 해당 worktree에서만 수행했다. 커밋·push·PR과 실제 인프라 실행은 하지 않았다.

## 완료한 연결

1. `core/storage.py`는 AST에서 `IMG_DIR` 리터럴, 환경변수 읽기, 해당 경로로의 파일 쓰기를 찾는다. 결과에는 `file`, `line`, `kind`만 담는다.
2. 분석은 원본 트리 전체에서 증거를 찾고 `IMG_DIR`를 plain으로 분류한다. 소스 필요 여부와 클라우드의 `upload_bucket` 출력으로 create / 유지 / remove를 구분한다.
3. cloud/both의 create·remove는 `infra_inputs_changed`를 켜고 다음 컨텍스트를 만든다. 저장된 프로젝트 설정으로 승인 스냅샷을 다시 만들 때도 이 내부 필드를 보존한다.

   ```text
   context.project_settings["_infra_storage"] = {
     "intent": "create" | "remove",
     "evidence": [{"file": ..., "line": ..., "kind": ...}],
     "bucket": "ddak-<platform>-uploads-<account>"
   }
   ```

4. `local_storage_dir`는 기존 의도 JSON → 원본 기반 렌더 → `check_patch` 흐름에 연결했다. `IMG_DIR = "img"`를 기본값 없는 `os.environ['IMG_DIR']`로 바꾼다. 검사기의 import·편집 허용 경계는 바꾸지 않았다.
5. 사용자 승인 사항인 app 층 `upload_bucket` 출력을 추가했다. 허용 형식은 `ddak-[a-z][a-z0-9-]{0,36}-uploads-[0-9]{12}`다. 저장 출력 재검증도 같은 규칙을 적용한다. JSON 스키마 필드는 변하지 않았고 `make contracts`에서 스냅샷 일치를 확인했다.
6. 클라우드 WAS의 일반 env에는 `IMG_DIR=s3://<upload_bucket>/img`, 온프렘 WAS에는 `IMG_DIR=img`를 공급한다. 클라우드 시크릿 주입 대상에서 제외한다.
7. `o1_onprem.py`의 was·three 레이아웃은 WAS 복제본 3개다. 둘 다 `<project>-uploads` named volume을 `/app/img`에 마운트한다. 같은 WAS 호스트의 Docker 볼륨을 공유하는 구성이다.
8. storage 증거가 있으면 smoke의 `storage` 그룹을 추가한다. `V3.upload`는 유효한 작은 PNG를 multipart `image`로 올리고 새 이미지 경로를 여섯 번 GET한다. 여섯 응답 모두 200이고 원본 PNG와 SHA-256이 같아야 통과한다. 결과에는 조회·일치 횟수와 해시만 남긴다.

## 변경 파일

- 공용: `src/ddak/core/storage.py`(신규), `core/contracts/infra_outputs.py`, `core/patch_patterns.py`, `core/runtime_values.py`, `core/smoke.py`
- 분석·계획: `src/ddak/plan/analyze/logic.py`, `rules.py`, `plan/flow.py`
- 패치·승인 전달: `src/ddak/plan/patch/intents.py`, `generate.py`, `executor/service.py`
- 주입: `src/ddak/cloud/deploy/entry.py`, `onprem/deploy/config.py`, `provider.py`
- 스모크: `src/ddak/verify/smoke/logic.py`, `fake.py`
- 인벤토리: `harness/scripts/o1_onprem.py`
- 테스트: `harness/tests/unit/test_storage_v3.py`(신규), `unit/plan/patch/test_generate_intents_fix11.py`, `unit/onprem/deploy/test_onprem.py`
- 이 인계서

## 실행한 검증

worktree 루트 기준이다. 테스트는 가짜 제공자·HTTP/Docker/SDK 대역과 임시 Git 저장소를 사용한다.

```sh
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest \
  harness/tests/unit/test_storage_v3.py \
  harness/tests/unit/plan/analyze/test_analyze.py \
  harness/tests/unit/plan/patch/test_intents.py \
  harness/tests/unit/plan/patch/test_generate_intents_fix11.py \
  harness/tests/unit/plan/patch/test_patch_check.py \
  harness/tests/unit/core/test_patch_patterns_fix11.py \
  harness/tests/unit/cloud/deploy/test_entry.py \
  harness/tests/unit/onprem/deploy/test_onprem.py \
  harness/tests/unit/onprem/deploy/test_three_tier.py -q
```

- 437 passed / 4.24초. 신규 create·유지·remove, 온프렘 단독, 의도 패치, 양쪽 env, 승인 컨텍스트 보존, 복제본 볼륨, 조회 누락·다른 바이트 실패를 포함한다.
- 초기 실패는 인벤토리 초기화 함수 반환값을 잘못 읽은 새 테스트 2건과 기존 env 테스트의 첫 줄 가정 1건이었다. 실제 JSON 인벤토리와 env 키 이름을 검증하도록 수정했다.

```sh
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest \
  harness/tests/unit/verify/smoke/test_smoke.py -q \
  -k 'fake_adapter or v2_group or smoke_opens or ready_matches or page_fingerprint or no_scenarios or passed_is_exactly'
```

- 21 passed, 26 deselected / 0.34초. 실제 HTTP 연결 테스트는 실행하지 않았다.
- PNG 청크 CRC 검증 보강과 저장 증거 AST 타입 정정 뒤 신규 파일을 다시 실행: 23 passed / 0.99초.
- `UV_NO_SYNC=1 PYTHONPATH="$PWD/src" make -C harness fmt`: 통과.
- 공용 변경 4파일 `storage.py`, `contracts/infra_outputs.py`, `runtime_values.py`, `smoke.py`의 scoped pyright: 0 errors.
- `UV_NO_SYNC=1 PYTHONPATH="$PWD/src" make -C harness contracts`: 스냅샷 일치. 재생성 불필요.
- `git diff --check`: 통과. 전체 CI는 실행하지 않았다.

## 통합 시 남은 확인

- 인프라 작업은 `_infra_storage`와 `upload_bucket`을 받아 생성·삭제·출력 갱신을 연결해야 한다. 해당 `cloud/infra/**`, `app.py`, 웹 코드는 여기서 수정하지 않았다.
- 기존 VM 인벤토리에는 새 템플릿이 자동 적용되지 않는다. 시연 전 WAS `volumes`에 공유 마운트를 반영해야 한다. 앱 이미지가 `/app/img`를 UID 10001 소유로 준비하는지도 앱 작업과 함께 확인한다.
- 여섯 번의 조회는 불일치를 발견하는 검사다. 실제 로드밸런서가 모든 복제본으로 분산했음을 증명하지는 않는다. 실제 S3·VM·업로드의 성공 및 시간은 통합 리허설에서 측정해야 한다.
- S3 이름 63자 제한을 지키기 위해 플랫폼 이름은 이 출력에서 최대 37자다. 현재 시연 플랫폼 이름에는 영향이 없다.
- NEEDS_CONTEXT: 추가 결정이 필요한 항목 없음. 인프라·앱·화면의 다른 작업 결과를 합친 뒤 실환경 검증이 남는다.
