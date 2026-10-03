# 2026-10-03 O2 범위 승계와 수정12

사용자 결정: O2 전체(plan/intake/detect/analyze/planner/validate/flow, core/ai, onprem/provision/inventory)를 O1 정준우가 승계한다. 준석 확인을 기다리지 않는다. 기존 C1 생성기 분담은 별도이며 이번 요청 밖 기능을 새로 구현하지 않는다. 영향받는 O2/O3/C3에는 변경 목록을 공유한다. 계약 변경과 `make contracts-update`는 승인됐다.

작업 기준은 `origin/main`의 `971c8e64e0d2ba665663183287c9d116214b4c42`, 브랜치 `o1/fix12-o2`, 독립 워크트리 `.worktrees/fix12-o2`다. 최초 Git 읽기로 확인했다. main 수정11은 동시 진행 중이며 다른 워크트리는 수정하지 않는다. TDD, 실제 AWS/VM/Docker/Claude/LLM 호출, 프로세스 kill, 저장소 commit/push/PR은 수행하지 않는다.

## P0 계약과 동작

- `Facts.smoke_groups` 및 `AnalyzeProjectOutput.smoke_groups`: 빈 tuple 기본. 분석은 새 배포 소스의 선언 tier 경로 내 템플릿에서 `class="release-box"`를 찾으면 `("v2",)`를 반환한다. diff나 이전 run에서 그룹을 이월하지 않는다. 표식과 허용 그룹은 AI 없는 `core/smoke.py` 한 곳에서 공유한다. O3 verify 코드는 표식 상수 import만 바꾼다.
- 검증기는 각 환경의 필수 smoke step에 `scenarios=["base", *valid_groups]`를 넣는다. 허용 그룹은 현재 base/v2이고 중복은 제거한다. 미지 그룹은 버리고 `unknown_smoke_group` 경고를 남긴다. v1→v2→v1은 base→base+v2→base다.
- step에는 `by`, 표시용 `reason`, 실제 사용 Facts를 가리키는 `evidence`를 채운다. 예: `fact:tree_changed.was`. optional AI 이유를 유지하되 등록되지 않은 선택 툴은 기존 규칙대로 제외한다.
- `approval_view.decision_basis`는 planner(provider/model/source/attempts/fallback), 포함 step의 by/reason/evidence, skipped/invalidated/warnings, `env_classification.by_provider`와 필요한 Facts 값을 반환한다. EnvKey.source도 기록해 replay 출처를 보존한다. 키 분류는 기존 `facts.json`에서 읽으며 키 이름/종류/판정 출처만 선택한다. 기존 기록에 facts가 없으면 `facts_available=false`로 표시한다. 기존 view export도 조회 시 근거를 다시 채운다. UI에는 판정 근거 카드 하나만 추가한다.
- `stage.finished` 이벤트에 `preparation_stage`, `elapsed_ms`(0 이상), 기존 `elapsed_s`를 기록한다. intake/detect/analyze/plan/validate/source/prepare의 성공·실패를 측정한다. 재계획·재검증도 각각 기록한다. 비밀값·코드·예외 원문은 넣지 않는다. 실행 이벤트 번호는 준비 이벤트 뒤에서 이어진다. 준비 이벤트만으로 배포 상태를 NEEDS_HUMAN으로 오판하지 않는다.
- replay fixture는 합성 응답이며 실제 모델 평가 증거가 아니다. 판단 결과의 `source=replay`를 analyze와 planner 끝까지 유지한다. 실제 기본 Claude CLI 선택/모델/설정은 바꾸지 않는다. fixture 누락·손상은 기존 보수 분류 또는 빈 규칙 계획으로 fallback한다.

## P1

P0 검증 완료 후 아래를 구현했다.

- flow와 detect는 요청 target의 previous만 비교한다. local 단독이면 cloud의 None이 changed_paths·new_migrations·env is_new를 전부 신규로 만들지 않는다. both 요청에서 성공 없는 cloud가 있다면 기존대로 해당 대상의 전체 신규 판정은 유지한다.
- Watcher는 마지막 성공이 없으면 첫 HEAD부터 계획을 요청한다. 기존 처리 기준선이 있어도 성공 여부를 app이 주입한 service.get_state로 확인한다. `DDAK_WATCH_INITIAL_TRIGGER=false` 또는 생성자 initial_trigger=False로 초기 기준선만 기록하는 동작을 선택한다. 감시 대상/브랜치/기본 주기는 사람이 정한다. 자동 승인·배포는 하지 않는다.
- 설정/계획/전제조건/응답 형식 실패는 한 번 처리한다. 일시적 AI_UNAVAILABLE/ADAPTER_TIMEOUT/ADAPTER_FAILED/INTERNAL/LOCK_HELD는 기존 상한 3회 재시도한다. 잘못된 SHA·비유한 주기 값은 거부하고 오류 원문은 감시 로그에 넣지 않는다. 결정적 polling 오류가 난 대상은 수정한 설정으로 watcher가 재시작해야 다시 확인한다.
- 함수 본문에서 매개변수를 os.environ[]/get/getenv로 읽는 같은 파일 top-level wrapper의 문자열 literal 호출을 AST로 수집한다. os/from os 별칭과 positional/keyword를 지원한다. nested 함수의 읽기를 outer 함수 근거로 오인하지 않는다. 외부 모듈·간접 wrapper chain·동적 문자열은 지원하지 않으며 값/기본값을 AI에 보내지 않는다.
- analyze README의 secret 승격만 가능하다는 과거 설명을 제거했다. 이름 규칙이 판정하지 않은 키는 실제 판단 모델이 plain/secret 중 제안하고 실패하면 보수 secret이다. 규칙 판정은 AI가 뒤집지 않는다.

## P2 설계 메모만 — 구현 금지

- db tier: build 대상에서 제외하고 DB 준비/접근·마이그레이션을 앱 배포보다 먼저 수행한다. R-migration은 끌 수 없고 신규 마이그레이션을 모든 요청 대상에서 앞 순서로 검증해야 한다. db build 제외와 순서 변경은 이번 코드에 넣지 않는다.
- ChangeFact: AI가 후보 사실을 추출 → 코드가 소스 위치/값 없는 근거를 검증 → 검증된 사실에 규칙 추가 → 사람이 승인. AI 실패·근거 불충분은 empty fallback이며 실행 규칙을 임의 추가하지 않는다. 구현하지 않는다.
- Jev는 발표에서 판단 역할 이름이다. TypeSafe는 예약된 통합이며 현재 사용했다고 주장하지 않는다. 실제 판단 provider/model/source와 발표 명칭을 구별한다.
