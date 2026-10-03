# 관리 웹 가독성·파이프라인 서술 인계 — 2026-10-03

작업 위치: `/Users/joonwoojung/Desktop/01_workspace/04_SoftBank_hackerton/.worktrees/ui-narrative` · 브랜치 `o1/ui-narrative` · 기준 커밋 `fdc2103` · 2026-10-04 갱신. 구현과 관련 검증을 마쳤다. 실제 외부 배포는 실행하지 않았다.

## 현재 반영 내용

- **공통 표현:** `web/narrative.py`의 작업 사전을 승인·진행·결과에서 공유한다. 1안의 살구색 바탕·밝은 제목 띠·1px 구분선으로 통일하고 녹색 성공 박스는 제거했다. 상태는 색과 글자·아이콘을 함께 쓴다.

| 페이지 | 표면 | 본문 | 보조 글씨 | 강조 | 성공 바탕 |
|---|---|---|---|---|---|
| `#fff2e8` | `#fffaf6` | `#36291f` | `#72513c` | `#ab382c` | `#fff2e8` |

- **승인:** 들어온 것 → 찾은 것 → AI가 고친 것 → 배포 계획 → 생기거나 바뀌는 리소스 → 승인 대상의 6개 섹션이다. 실제 티어·계획의 포함/제외 작업과 이유를 보여 준다. 리소스 생성/수정/삭제/교체 4개 타일과 IAM 역할 카드를 제공하고 ARN·정책은 접힌 상세에 둔다. S3 근거가 있을 때만 탐지 → 필요 판단 → Terraform 생성/복구 → 승인 요청 → 승인 후 적용의 5단계를 표시한다.
- **코드 표시:** `core/code_mask.py`를 승인·패치 검토에서 공유한다. 원본·수정 파일 전체를 AST/token으로 가린 뒤 변경 줄을 추출한다. 키 이름·코드 구조·ProxyFix 변경 맥락을 남기고 문자열 값·숫자·주석·여러 줄 문자열을 가린다. 파싱 실패 시 모든 줄을 가린다. `display_data`는 자식의 `patch_targets`가 없으면 부모 facts에서 보충하며, 재계획 자식 facts는 `service.prepare` 이후 저장한다.
- **진행:** 실제 plan step을 SSR로 먼저 표시하고 기존 SSE로 상태를 갱신한다. 이벤트 처리 중 DOM 재조회는 0회이며 노드를 초기화 때 저장한다. 현재 작업·전체 경과 시계, 이전 성공 이력이 있을 때만 시간 추정, glide+wave 동작을 적용했다. 동작 줄이기 설정에서는 이동 대신 opacity 변화만 쓴다. 완료 작업 설명과 원문 로그는 접힌다. 종료만으로 미기록 작업을 성공 처리하지 않는다.
- **환경별 결과:** 온프레미스·클라우드는 독립 레인으로 진행한다. 먼저 검증된 환경은 `local_verified`/`cloud_verified` 시점에 결과 카드를 갱신하고 다른 환경은 계속 진행한다. 읽기 전용 `track_result`가 표시 데이터를 제공하며 진행·최종 결과는 `_environment_cards.html`을 공유한다. 두 환경의 검증 통과 후 비교·보고한다.
- **결과:** 상태 배너를 맨 위에 두고 시간·이미지·환경·검증 지표와 환경 카드를 보여 준다. 요약은 결론+변경 항목 최대 3개 칩으로 압축했다. 보고 프롬프트는 결론 30자, 변경 항목 2개·각 40자 이내를 요청하며 기존 스키마를 유지한다. 단계 기록·오류 원문·JSON은 접힌 상세에 둔다.
- **설정:** 연결 점검표는 `/settings`에만 둔다. 대시보드는 문제가 있을 때 설정으로 이동하는 안내 한 줄을 표시하고 연결 상태 갱신을 유지한다.

## 유지한 동작과 계약

`code_qa` 질문, POST 오류·입력 유지·CSRF, `patch_lost` 승인 차단, 대상별 승인 해시를 유지한다. 승인 폼은 하나이며 상단·하단에 같은 결정 컨트롤을 둔다. 승인 별칭 HTML과 기존 `/progress`·`/runs/<id>` 동선을 유지한다. 툴 이름·파라미터·스키마·이벤트·카탈로그·배포 설정 키는 불변이다. 웹 층에서 AI를 호출하지 않는다.

## 검증 인계

로그 위치는 모두 `harness/var/validation/ui-narrative/`이다.

- 최종 화면 회귀: `final-render.txt` — **70 passed, 0 failed (1.83초)**. 승인·진행·결과, 두 환경 결과 복원, 마스킹, S3 생성/삭제·없음, SSE 무조회, 기존 HTTP 동선을 포함한다.
- 직전 관련 전체 묶음(`final-related.txt`)은 163개 중 162개가 통과했다. 실패 1개는 최신 S3 삭제 문구와 이전 기대값의 차이였다. 기대값을 최신 지시에 맞춘 뒤 해당 파일을 포함한 최종 70개가 모두 통과했다. 나머지 93개의 패치 조합·폼·요약 테스트는 이 직전 실행에서 통과했으며 이후 해당 코드 변경은 없다.
- 가짜 생성 호출 테스트는 정상 요약·시간 초과·상태 불변·비밀 원문 비입력을 확인했다.
- `make -C harness fmt`, `git diff --check` — 통과. 대비 계산은 `contrast.json`: 사용한 본문/보조·상태·diff·CTA 조합 최소 **4.815:1**.

최종 렌더 명령:

```sh
UV_NO_SYNC=1 PYTHONPATH="$PWD/src" .venv/bin/python -m pytest \
  harness/tests/unit/test_ui_narrative.py \
  harness/tests/unit/test_ui_storage_narrative.py \
  harness/tests/unit/test_deployment_story.py \
  harness/tests/unit/test_ui_redesign.py \
  harness/tests/unit/test_e2e_ux1.py \
  harness/tests/unit/test_int1112_ui.py \
  harness/tests/unit/test_ui_integration_fix10.py -q --tb=short
UV_NO_SYNC=1 PYTHONPATH="$PWD/src" make -C harness fmt
git diff --check
```

직전 묶음에는 위 범위와 `test_ui_flow`, `test_ui_sidebar`, `test_ui_patch_review`, `test_patch_review_assembly`, `test_patch_review_form_errors`, `verify/report/test_report_summary`를 포함했다. 전체 CI·gitleaks·독립 리뷰는 속도 우선 지시에 따라 생략했다.

브라우저에서는 승인·진행·결과 모두 1440·1280·390px에서 `scrollWidth == innerWidth`를 확인했다. 작은 폭은 가로 넘침만 확인했다. 진행 점은 실제 계산 스타일에서 `lane-glide`, `work-wave`가 적용됐다. 결과는 1440px에서 환경 카드 2열이다.

1440px 캡처 3장:

- `harness/var/validation/ui-narrative/narrative-approval-1440.jpg`
- `harness/var/validation/ui-narrative/narrative-progress-1440.jpg`
- `harness/var/validation/ui-narrative/narrative-result-1440.jpg`

`preview/`의 HTML·정적 파일과 `render_preview.py`는 가짜 기록으로 만든 검증 자료다. 실제 배포 증거가 아니다. 임시 정적 서버는 종료했다. 실제 AWS·VM·Docker·Terraform·생성 서비스 호출, 커밋·push·PR·Git 설정 변경, 사용 기록 문서 수정은 없다.

## 변경 파일 그룹

현재 `git status --short --untracked-files=all` 기준이다. 아래 신규 표시는 미추적 파일을 뜻한다.

- 공통 가림: `src/ddak/core/code_mask.py` **신규**.
- 실행 연결: `src/ddak/app.py`, `src/ddak/executor/{patch_review.py,presentation.py,service.py}`.
- 보고: `src/ddak/verify/report/{logic.py,prompt.md}`.
- 웹 데이터·경로: `src/ddak/web/{dependencies.py,story.py,narrative.py(신규)}`, `src/ddak/web/routes/{events.py,pages.py,patches.py,results.py,settings.py}`.
- 정적 파일: `src/ddak/web/static/{app.css,app.js}`.
- 기존 템플릿: `src/ddak/web/templates/{_demo_reset.html,_status.html,approval.html,base.html,dashboard.html,overview.html,patch_review.html,progress.html,result.html,settings.html,setup.html}`.
- 신규 템플릿: `src/ddak/web/templates/{_code_change.html,_environment_cards.html,_pipeline.html,_resources.html}`.
- 관련 테스트 변경: `harness/tests/unit/{test_deployment_story.py,test_e2e_ux1.py,test_int1112_ui.py,test_patch_review_assembly.py,test_ui_flow.py,test_ui_patch_review.py,test_ui_redesign.py,test_ui_integration_fix10.py}`, `harness/tests/unit/verify/report/test_report_summary.py`; `harness/tests/unit/{test_ui_narrative.py,test_ui_storage_narrative.py}` **신규**.
- 인계 문서: `harness/docs/guides/O1-ui-narrative-handoff-2026-10-03.md` **신규**.

미추적 `.venv`는 실행 환경 항목으로 구현 변경 목록에서 제외한다.

## 후속 · NEEDS_CONTEXT

- 저장 기록에 별도 v1/v2 버전이 없으면 확인된 커밋만 확정 표시한다. 버전명을 추정하지 않는다.
- 준비·패치·인프라 세부 시작/완료 이벤트는 없고 현재 큰 단계 완료(`stage.finished`) 기록만 있다. 승인본 소스 준비 완료는 실행 step 시작에 따른 추정이며 확정 이벤트가 아니다.
- 컨테이너 대수·작업 내부 완료율 입력은 없다. 필요한 경우 비밀값 없는 집계와 이벤트 계약을 먼저 정해야 한다.
- `track_result`의 안전한 상세 출력은 프로세스 내 캐시다. 페이지 새로고침에서는 복원되지만 프로세스 재시작 후에는 상세 스모크 시나리오·통과 수를 복원하지 못한다.
- **S3 저장 계약 연동:** main(#44)에서 `_InfraSummary.storage`가 연결됐다. 승인 화면 S3 블록은 실제 승인 데이터(판단 흐름 5단계·버킷·환경 키)를 읽고 `test_storage_approval.py`가 생성·삭제 렌더를 확인한다. UI 단위 테스트는 같은 모양의 fixture view를 쓴다.
- S3 생성/삭제는 계획·현재 작업·클라우드 레인·대시보드 준비 목록까지 연결했다. bucket 누락·일반 인프라와 S3가 함께 바뀌는 경우, 삭제 후 다음 단계의 경고색 전환은 후속 회귀 범위다.
- 값만 달라진 수정은 마스킹 후 전후 코드가 같은 줄로 보일 수 있다. 변경 강조 보완은 후속 작업이다.

## 2026-10-04 · 1안 확정

기존 화면 구조를 유지하고 `src/ddak/web/static/app.css`의 배경·명암·제목 띠·구분선·모서리만 변경했다. 검정 상단 면, 환경 결과의 큰 외곽 채움과 회색 배경을 걷어냈다. 템플릿·JS·배포 로직은 이번 선택 적용에서 변경하지 않았다.

검증: `UV_NO_SYNC=1 PYTHONPATH="$PWD/src" .venv/bin/python -m pytest harness/tests/unit/test_ui_narrative.py -k all_text_tokens_meet_aa_on_used_surfaces -q --tb=short` → 1 passed, 9 deselected. `git diff --check -- src/ddak/web/static/app.css` 통과.

확정 화면: `.orchestrator/design/mockups/ac-refresh/captures/selected-a-result.jpg`. 현재 브라우저는 이 CSS를 복사한 정적 미리보기이며, 실제 서버 재시작·배포는 수행하지 않았다. 이 시각 변경의 후속 작업은 없다.

## 2026-10-04 · 기능 보완

1안의 색·구조를 유지하면서 대조표에서 지정한 기능 결함을 보완했다.

- 결과: 성공 시 건너뛴 원인 분석은 `— 원인 분석: 실패가 없어 생략`으로 표시한다. 사용자 시나리오를 N/N으로 묶고 검증 중복을 제거했다. 다음 행동은 최대 하나이며, 환경별 서비스 링크는 해당 환경이 완료된 경우만 표시한다. 기록된 버전·커밋과 새 빌드·재사용 행을 환경 카드에 담았다.
- 승인: 문장 전체와 이유 코드를 사전에서 찾고, 미등록 한국어 문장은 비밀값을 가린 뒤 보존한다. 기술 정보 JSON은 한국어를 유지한다. 코드 가림은 비밀값·주소·문자열·숫자·주석으로 구분하고 해석 불가 줄은 `[가림 · 코드 줄]`로 표시한다. 승인 주체 배지는 `사람 승인`이다.
- 진행: SSE 갱신에서 상태 아이콘을 유지한다. 시각은 KST `HH:MM:SS`, 완료 행은 제목·완료 문장·상태를 한 summary로 접고 주체·툴·시간은 펼칠 수 있다. S3 삭제 경고색과 연결 단절 시 동작 중지·재연결 시 진행 행만 재개를 적용했다.
- 대시보드: 실제 클라우드 인프라 계획이 있으면 레일의 `클라우드 구성`을 표시한다. 준비 실패 안내는 오류 사전 문장으로 바꿨다.

### 레인 시계·변경 수 합의

- 서버는 해당 환경의 기록된 `step.started` 중 가장 이른 `ts`를 `pipeline.lanes_progress[track].started`에 담는다. 템플릿은 이를 `data-lane-started`로 전달하며 SSE도 기존 `step`·`target`·`ts`만 사용한다. `lane_started` 별도 필드나 새 이벤트는 없다.
- 현재 `ApplyInfraOutput`에는 `passed`, `layer`, `plan_sha256`, `outputs`, `elapsed_seconds`, `source`, `boundary_versions`가 있으며 변경 집계 필드는 없다. 표시용 캐시에도 `passed`·`source`·검증 시나리오만 남고 SSE에는 counts가 없다.
- 완료 문장의 `(변경 N)`은 `output.counts`의 `create/update/delete/replace`에 유효한 정수가 기록된 경우에만 붙는다. 실제 현재 경로에서는 집계가 없으므로 생략한다. SSR 기록의 숫자는 같은 시작 시각의 SSE 재생에서 보존하고 새 실행에서는 이어받지 않는다. 계획 숫자를 실제 적용 숫자로 대체하지 않는다.

### 이번 변경 파일

- 서버: `src/ddak/{core/code_mask.py,executor/presentation.py,web/narrative.py,web/story.py,web/dependencies.py,web/routes/events.py,web/routes/pages.py}`.
- 화면: `src/ddak/web/templates/{approval.html,dashboard.html,progress.html,result.html,_environment_cards.html}`, `src/ddak/web/static/{app.js,app.css}`. CSS는 진행 상태·애니메이션 선택자만 추가 조정했다.
- 테스트: `harness/tests/unit/test_ui_explanations_followup.py`, `test_ui_result_followup.py`, `test_ui_progress_followup.py` 신규 및 `test_deployment_story.py`, `test_ui_flow.py`, `test_ui_integration_fix10.py`, `test_ui_patch_review.py`, `test_ui_redesign.py` 기대값 갱신.

### 이번 검증

```sh
UV_NO_SYNC=1 PYTHONPATH="$PWD/src" make -C harness fmt
UV_NO_SYNC=1 PYTHONPATH="$PWD/src" .venv/bin/python -m pytest \
  harness/tests/unit/test_ui_explanations_followup.py \
  harness/tests/unit/test_ui_result_followup.py \
  harness/tests/unit/test_ui_progress_followup.py \
  harness/tests/unit/test_deployment_story.py \
  harness/tests/unit/test_ui_narrative.py \
  harness/tests/unit/test_ui_storage_narrative.py -q --tb=short
UV_NO_SYNC=1 PYTHONPATH="$PWD/src" make -C harness check
```

- 관련 테스트 **68 passed (1.52초)**. `harness/var/validation/ui-narrative-followup/related.txt`에 저장했다.
- `make fmt`와 `git diff --check` 통과. 새 테스트의 긴 JS 문자열을 줄바꿈한 뒤 확인했다.
- `make check`: **4298 passed, 28 failed, 1 skipped, 4 deselected**. 출력은 `harness/var/validation/ui-narrative-followup/check.txt`에 저장했다. 형식·타입·경계·계약·이력 검사는 통과했고 테스트 단계가 실패했다.

```text
lint         PASS     0.0s
type         PASS     2.0s
boundary     PASS     0.4s
contracts    PASS     0.2s
test         FAIL   351.1s
attribution  PASS     0.1s
합계 353.8s
```

28개 실패 중 이번 표시 변경에 해당하는 기대값 7개를 갱신했다. 준비 실패의 사람 문장 2개, 결과 서비스 링크 2개, 한국어 가림 표기 2개, 상태 아이콘 옆 라벨 1개다. 기존 비노출·폼·HTTP 검증은 유지했다.

위 관련 6파일에 `test_ui_flow.py`, `test_ui_integration_fix10.py`, `test_ui_patch_review.py`, `test_ui_redesign.py`를 더한 최종 검사: **135 passed (10.44초)**. 명령은 앞 pytest 명령에 이 네 파일을 추가한 것으로, 출력은 `harness/var/validation/ui-narrative-followup/related-final.txt`다. 이후 `make -C harness lint`와 `git diff --check`도 통과했다. 전체 check는 다시 실행하지 않았으므로 전체 통과로 보고하지 않는다.

나머지 21개는 별도 반영 브랜치 담당 범위다: `test_bootstrap_dbinit_policy` 1개, `test_boundary_views` 3개, `test_demo_reset` 1개, `test_first_run_connections` 2개, `test_form_submit_js` 7개, `test_ui_settings_fix10` 7개. 이 파일들은 수정하지 않았다.

### 2026-10-04 · 가로 단계 띠 제거·요약 전체 표시

최신 화면 피드백에 따라 대시보드·승인·진행·결과 템플릿의 공통 가로 단계 띠 호출을 제거했다. 환경별 실제 진행 레인은 유지한다. 선택 화면의 정적 목업 A/C에도 같은 제거를 적용했다.

`narrative.short_summary`의 56자 절단을 제거하고, 식별자 정리만 유지한다. 요약 항목은 전체 문장을 표시하고 폭을 넘으면 줄바꿈한다. 최대 3개 항목 구성은 유지한다. 이 결정이 이전 단계 띠 표시·요약 56자 제한 요구를 대체한다.

검증: `test_ui_narrative.py`, `test_ui_explanations_followup.py`, `test_ui_result_followup.py`, `test_deployment_story.py` **48 passed (0.60초)**. 긴 요약의 마지막 문장이 보존되고 강제 말줄임표가 없는지 확인했다. `make -C harness lint`, `git diff --check` 통과. 현재 정적 결과 미리보기는 HTTP 200이며 `data-pipeline-rail`이 없다. 실제 관리 서버 재시작은 수행하지 않았다.

### 후속 작업

- 실 적용 변경 수를 표시하려면 툴 출력·저장·표시 경로의 집계 제공이 필요하다. 이번 작업에서는 계약을 확장하지 않았다.
- 이전 릴리스의 `ref`·`version`을 표시 데이터로 보존한다. 명시된 v1/v2 기록이 없으면 `버전 기록 없음`으로 표시하며 추정하지 않는다.
- 사용자가 별도 반영 브랜치에 맡긴 기존 22개 회귀는 이 작업에서 변경하지 않았다. S3 저장 계약 연동도 해당 반영 시 확인이 필요하다.
