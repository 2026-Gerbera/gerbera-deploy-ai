# 후속 수정 6 인계 — 2026-10-02

기준 `main 5b107a0`, 작업 브랜치 `o1/exec-fix6`. 작업 사본만 수정했다. commit/push/PR, AWS/VM 접속, 프로세스 종료 명령은 실행하지 않았다. 별도 문서 브랜치는 건드리지 않았다.

## 결과

| 항목 | 상태 | 구현·검증 |
|---|---|---|
| 1 경로 | DONE | 설정을 읽는 시점의 실행 디렉터리(make 진입점은 harness)를 기준으로 절대화. Settings 경로·서비스 root/SQLite·FetchPolicy root·PlanBundle source·repository factory 경계를 고정. 기본 상대 설정으로 로컬 bare Git v1→v2→v1 및 승인 대기 재시작 통과 |
| 2 미빌드 이미지 | DONE | prepare에서 대상별 마지막 성공 digest/source_mode 확인. 승인 스냅샷에 기준 기록을 보존하고 start에서 변경 재검사. 각 툴의 대상별 컨텍스트에만 이월하여 병렬 local/cloud 값 혼합 방지. naked images 덮어쓰기·NEEDS_HUMAN 기준 사용 거부. 이월 관측값은 이전 artifact로 검증하고 image_sources.carried_forward에 기록 |
| 추가 A ID | DONE | 마이그레이션은 deploy.migrate.local/cloud, MySQL 컨테이너는 deploy.db.local. 카탈로그·주석·검증기의 문자열 ID·golden·테스트 교정, contracts-update 수행 |
| 추가 B O1 DB 연결 | DONE / O2 자동 계획 연결 필요 | 기존 CLI의 images.lock.json/mysql 형식 재사용(선택 나). 승인된 수정본에서 공식 MySQL ref/index/두 플랫폼 digest 검증 후 전달. 새 앱 이미지 빌드 뒤에도 DB artifact 보존. 수동 FAKE 3티어 계획의 DB→migrate→WAS→web 실행 통과. 기존 provider의 DB·볼륨 보존 경로는 유지 |
| 3 FAKE 인프라 | DONE | FAKE 조립에서만 source=fixture 번들·CLI/SDK 경계를 주입. 일반 InfraRuntime의 validate/plan/승인 해시·잠금/apply 경로 유지. cloud/both 초기 실행 통과. REAL fixture 사용 거부, REAL 생성기 미등록 오류 유지 |
| 4a 인벤토리 | DONE | REAL local prepare에서 인벤토리 및 배포/마이그레이션 tier 포함 여부 검사. 앱 조립의 load_inventory가 상세 모델 검증 담당 |
| 4b facts | DONE | 성공·준비 실패 run에 facts.json(0600) 저장. 환경 키의 자유 서술 reason은 제거하고 나머지는 redact. 이름·종류·변경 여부·해시만 진단에 사용 |
| 4c 사본 정리 | DONE(기존 경로 확인·회귀) | intake의 var/sources/runs/project/run_id는 기존 cleanup_stale_sources TTL 대상. 서비스 승인 사본 var/sources/run_id는 분리 보존. 실패 시 이번 요청이 만든 승인 사본만 즉시 정리. TTL로 원본 접수 사본이 사라져도 승인 조회 유지 |
| 5 요청 목록 | DONE | 아래에 담당자별 기록. 해당 기능 코드는 수정하지 않음 |

`RunContext`/DeployConfig 필드는 추가하지 않았다. 기존 source-bound ReleaseArtifacts로 초기 배포하는 운영 CLI 경로도 유지한다. `images` 문자열만으로 이월 기준을 덮어쓰는 것은 허용하지 않는다.

DB digest는 인벤토리에 이미 있는 값이 아니었다. 현행 provider에는 이미지 필드가 없고 온프렘 CLI가 `images.lock.json`을 읽는다. 따라서 새 DeployConfig 필드 없이 그 형식을 재사용했다. lock 파일이 없거나 공식 digest가 아니면 승인 전에 거부한다. 직접 주입한 승인 artifact도 공식 MySQL이어야 한다. 빌드 계획의 `build.db`와 local 공식 DB 이미지를 cloud의 `deploy.db`에 공유하는 계획은 거부한다. DB 신규 생성·기존 볼륨 보호의 실제 VM 재실행은 이번 범위 밖이다.

## 담당자 요청 목록

- **준석 확인 필요(10/2 정준우 결정):** `plan/validate/rules.py`의 `deploy.db.<env>` 문자열만 `deploy.migrate.<env>`로 변경했다. README·관련 테스트도 같은 변경이다. 자동 계획 로직은 수정하지 않았다.
- **준석/O1 공유 카탈로그 후속:** 온프렘 MySQL `db`를 `build.db`/Dockerfile 생성 대상에서 제외하고 `deploy.db.local(deploy_tier)`에는 포함해야 한다. `core/contracts/step_catalog.py`의 tier 순회와 O2 규칙을 함께 맞춘다. 순서는 config 주입 → DB 컨테이너 → dbinit/마이그레이션 → WAS → web. `R-migration`의 "모든 deploy_tier보다 먼저" 조건은 DB 컨테이너만 예외로 해야 한다. 현재 검증기는 DB 선행을 거부하므로 자동 3티어 계획 E2E 완료로 보고하지 않는다. 클라우드 DB는 로컬 MySQL 이미지를 재사용하지 않고 클라우드 인프라 경로와 역할을 별도로 조립해야 한다.
- **준석:** `plan/flow.py`의 previous_manifests를 요청 대상 환경으로 한정. 온프렘 단독인데 배포하지 않을 cloud 기록 부재까지 비교하면 전체 변경으로 판정된다.
- **준석:** Jev 설정 안내에 `DDAK_JEV_MODEL` 필요 명시. Groq 실행의 출처가 `claude-api`로 남는 표기 수정. 등록되지 않은 선택 툴 `prepare_storage`를 validate_plan에서 거부.
- **서윤:** 온프렘 단독 성공 보고의 high `cloud_domain_missing` 경고 제거. 이번에는 web/verify 코드를 수정하지 않았다.
- **민영 / PR #11 후속:** 별칭 import(`from os import system as getenv`) 검사 우회는 2차 전에 해결할 P2다. 정상 `SECRET_KEY` subscript·dict 수정 오탐, 두 줄 나누기, 주석 속 비밀값도 확인한다. 이번 O1 변경으로 해결했다고 보고하지 않는다.
- **NEEDS_CONTEXT — 민영·정준우:** 패치 생성기를 연결할 때 `prepare`가 `patch_meta`에 검사기 결과 `passed`, `patch_sha256`를 요구하도록 계약을 합의한다. 이번에는 승인 모델·O3 코드를 변경하지 않았다.

추가 A는 정준우의 확정 결정이며 추가 승인이 필요하지 않다. DB 빌드/정렬·cloud DB 표현은 위 요청대로 O2와 취합해야 한다. 수동 fixture 계획 성공을 자동 계획이나 실 VM 성공으로 바꾸어 표현하지 않는다.

## 검증과 커밋 제안

실행: `make -C harness contracts-update`, 핵심 경로 pytest, `make -C harness ci`. 같은 전체 CI를 gitleaks가 있는 PATH와 없는 PATH에서 각각 실행한다. 제품의 스캐너 fail-closed는 변경하지 않았다. E2E의 통과용 scanner는 source=fixture이며 탐지 능력 검증이 아니다.

로그·UTC 시작·소요 시간·종료 코드: `harness/var/validation/followup6-20261002/`. 초기 실패 로그도 별도 보존한다. CI와 로컬 bare Git 수치는 실제 배포 시간 벤치마크가 아니다. 최종 수치는 아래 검증 마감에 기록한다.

제안 커밋 메시지:

1. `fix: bind deployment paths and reuse approved per-target images`
2. `fix: separate migration steps and wire local MySQL and fake infrastructure`
3. `docs: record executor fix 6 validation and integration requests`

service.py/app.py/테스트의 변경이 겹치므로 위 1·2는 주제별 메시지 후보다. 파일만으로 분리하려면 코드·계약·테스트를 한 커밋으로 합쳐 `fix: close O2 to O1 deployment integration gaps`를 사용한다. 중간 커밋별 CI는 별도로 검증하지 않았다.


## 검증 마감

- gitleaks 있음: 전체 CI **PASS**, **1319 passed / 1 skipped / 4 deselected**, 전체 89.818초(pytest 87.72초).
- gitleaks 없음: 전체 CI **PASS**, **1317 passed / 3 skipped / 4 deselected**, 전체 90.563초(pytest 88.78초). 실제 Gitleaks 테스트 2개만 추가 skip. 90초 목표는 약 0.56초 초과했지만 CI 종료 코드는 0이다.
- lint/type/import boundary/contracts 모두 PASS. `git diff --check` 통과. contracts-update 후 변경은 마이그레이션 문자열·golden에 있으며 생성 스키마 내용 드리프트는 없다.
- 독립 검토: Pre REVISED → 지적 재확인·회귀 추가 → 최종 좁은 Post PASS. 검토 에이전트는 CI를 직접 실행하지 않았고 위 CI는 주 실행 세션에서 수행했다. 외부 Claude 검토나 실환경 E2E 완료를 주장하지 않는다.
- 시작 UTC/명령/스캐너 경로/종료 코드/시간은 `harness/var/validation/followup6-20261002/{with-gitleaks,without-gitleaks,summary}.json`과 로그에 보존했다. 초기 테스트 회귀 및 lint 실패 기록도 보존했다.
- 작업 종료 직전 계정 사용량 78%(잔여 22%)로 PAUSED 기준 21%에 도달하지 않았다. commit/push/PR은 사용자가 수행한다.

## 추가분 2 — apply_diff 형식 경계

`core/snapshots.py`와 `tests/unit/test_snapshots.py`만 추가로 수정했다. 제공된 `.orchestrator/applydiff_fix_draft.diff`를 검토했고, EOF 표시를 접두사 대신 문자열 전체 일치로 검사하도록 보완했다. 직전 hunk 본문 태그와 해당 쪽 남은 줄 수가 맞을 때만 표시를 허용한다. `/dev/null`, 탭 접미사, 경로 불일치, 미존재 파일, 중복 파일 헤더를 적용 전에 거부하고, 적용 뒤에도 파일 목록과 실제 변경 경로를 대조한다.

허용 범위는 기존 텍스트 파일의 내용 수정이다. Git 확장 메타는 계속 거부한다. 파일당 헤더 한 쌍·hunk 하나만 지원하므로, 정상 Git 다중 hunk 전체를 지원하는 파서로 설명하면 안 된다. Git으로 만든 EOF diff 테스트는 확장 메타를 제거한 뒤 본문과 EOF 표시를 그대로 사용하고, 승인 미리보기→빌드 사본의 최종 바이트와 해시를 비교한다.

검증 명령·시작 UTC·시간·종료 코드·로그는 `harness/var/validation/followup6-applydiff-20261002/`에 별도로 기록한다. 앞 절 CI 수치는 추가분 2 이전 결과다. 이번 결과는 아래 검증 마감으로 구분한다.

제안 커밋: `fix: restrict approved patches to existing files and valid EOF markers`


### 추가분 2 검증 마감 · PAUSED

- 집중 명령 `make -C harness test ARGS='tests/unit/test_snapshots.py tests/unit/core/test_candidate.py -q'`: **93 passed**, 27.186초(pytest 26.92초), exit 0. 정상 Git EOF diff 5종 포함.
- `make -C harness ci`, gitleaks 있음: **1342 passed / 1 skipped / 4 deselected**, 전체 **98.222초**(pytest 96.10초), exit 0.
- 같은 전체 CI, gitleaks 없는 PATH: **1340 passed / 3 skipped / 4 deselected**, 전체 **93.092초**(pytest 90.68초), exit 0. 실제 scanner 테스트 2개만 추가 skip. 제품 fail-closed는 유지했다.
- lint/type/import boundary/contracts 모두 PASS. 두 실행 모두 90초 목표를 넘었다. 수치는 테스트 실행 시간이며 배포 벤치마크가 아니다.
- Pre-mortem **REVISED**(Sagan), Post-gate **PASS**(새 검토자 Mill), External Claude **NOT_REQUIRED**, 이번 파서 증분 Overall **PASS**, 차단 발견사항 없음. 별칭 import 등 O3 수정은 사용자 요청대로 인계만 했으며 해결로 판정하지 않았다. 검토자는 코드를 읽고 집중 로그를 확인했고, 전체 CI는 주 세션에서 실행했다.
- 증거: `harness/var/validation/followup6-applydiff-20261002/{focused,with-gitleaks,without-gitleaks,summary,review-trace}.json` 및 각 실행 로그. 명령·UTC 시작·소요 시간·종료 코드를 보존했다.
- 구현·회귀 검증 후 잔여 사용량 **21%** 도달을 확인했다. 추가 개발을 중단하고 이미 시작한 검증 결과만 회수해 안전 지점에서 **PAUSED**한다. 코드 수정은 완료됐고 재개 시 새 작업 지시부터 진행하면 된다. commit/push/PR, AWS/VM 접속, 프로세스 종료 명령은 실행하지 않았다.
