# 관리 웹 B 스위스 원장 반영 인계

2026-10-03. 사용자가 3개 후보 중 **B 스위스 원장**을 선택했다. 라이트 테마와 기존 제품명 Gerbera를 유지한다. 작업 기준은 `origin/o1/ui-design-base`의 `6d1ef76996dd3830a21229f21d054e596cc884b9`, 작업 위치는 `.worktrees/ui-redesign`, 브랜치는 `o1/ui-redesign`이다. 커밋·push·PR·태그는 만들지 않았다.

## 바뀐 화면과 파일

카드 대신 가로선·표·정의 목록으로 구획을 나눈다. 주 버튼은 검정이다. 큰 둥근 모서리, 그림자, 그라데이션, blur, 장식 아이콘, 상시 애니메이션, 12px 미만 글자는 제거했다. 상태는 한국어와 원·마름모·사각형·삼각형·가로선으로 구분한다.

- `src/ddak/web/static/app.css`: B 토큰·레이아웃·반응형·3px 키보드 포커스. 기존 덮어쓰기 층을 교체했다.
- `src/ddak/web/static/app.js`: 준비 요청 상태, 거절·잠금 해제 확인, 설정 조건부 필수값, 저장 알림, 환경별 SSE 표시. 수신 이벤트만 반영하며 이전 구간을 임의로 완료하지 않는다.
- `src/ddak/web/templates/base.html`: 상단 메뉴와 CSS/JS 캐시 버전 `ledger-20261003`.
- `templates/dashboard.html`: 현재 단계·상태별 다음 행동·실행 표·연결 정보·접힌 운영 도구.
- `templates/approval.html`: 소스·환경·인프라·패치·상세 접기·승인 한 번. 거절은 보조 버튼으로 유지하고 확인을 받는다.
- `templates/progress.html`: 환경별 단계와 실제 이벤트, 종료 상태·결과 링크. 세 이벤트 목록 ID를 유지한다.
- `templates/result.html`: 실제 환경별 결과, 첫 실패 단계, 오류·인프라 경고·메모 열. 현재 서비스 SHA를 추정하지 않는다.
- `templates/settings.html`: 한국어 라벨, 온프렘 기본, 조건부 도메인·Route 53 필드, 저장 알림.
- `templates/ops.html`, `templates/_ops.html`: 준비·상태별 기록 링크·잠금 해제. 운영 화면의 잠금 해제는 기록 아래에 둔다.
- `templates/_public_links.html`: 안전한 공개 주소·버전 링크를 유지하며 문구만 정리했다.
- 신규 `templates/_status.html`: 상태·환경·단계 이름·시간 표시 매크로.
- `src/ddak/web/routes/pages.py`: 기존 실행 컨텍스트의 커밋·ref·대상·방식·시각을 표시한다. 시각은 KST라고 명시한다.
- `src/ddak/web/routes/ops.py`: 실행/준비 기록에 공통 `run_link` 적용. 준비 목록은 최신 요청부터 표시한다.
- `src/ddak/web/routes/events.py`: 진행 템플릿에 기존 컨텍스트의 대상만 전달한다. 이벤트 프로토콜은 바꾸지 않았다.
- `harness/tests/unit/test_ui_redesign.py`: POST 필드·거절·환경 결과·온프렘 승인 문구·실제 단계 DOM을 가진 node VM 회귀, 자유 형식 IAM 메타·실제 권한 생산 형식 회귀 13개.
- `harness/tests/unit/test_ui_integration_fix10.py`, `test_fullchain_fix8.py`: 아래 문구 기대값만 변경.
- 이 인계서와 `harness/docs/ai-usage/O1.md` append.

위 `templates/`는 모두 `src/ddak/web/templates/` 아래다. `.venv`는 저장소 루트의 환경을 가리키는 심볼릭 링크이며 추적하지 않는다.

## 폰트

B 선택은 시스템 산세리프 + 시스템 고정폭이다. A를 전제로 적힌 Pretendard / IBM Plex Mono 다운로드 지시는 이번 선택에 적용하지 않는다.

- 본문: `-apple-system, BlinkMacSystemFont, Segoe UI, Apple SD Gothic Neo, Malgun Gothic, sans-serif`.
- 코드: `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`. 합자 없음.
- 배포하는 폰트 파일 **0개, 0바이트**. CDN·원격 폰트 요청 없음. WOFF2·OFL·파일 SHA-256은 배포 대상이 없으므로 해당 없음.
- 폰트는 각 OS가 제공하는 설치 글꼴이다. 이번 변경이 OS 글꼴의 재배포 권한을 주장하거나 폰트 파일을 포함하지 않는다.
- 폰트 때문에 오프라인 표시가 막히지 않는다. OS마다 글리프 폭은 달라질 수 있다. 현재 실측은 macOS의 Codex 브라우저다.

## 보존한 동작

| 조건 | 확인 근거 |
|---|---|
| 상태별 실행 링크 | 대시보드·운영은 `run_link`, 승인/진행/결과 HTTP 회귀 |
| 실패 조건·인프라 경고·메모·원시 코드 | 기존 성공/실패·준비 실패 HTTP 검사, 새 결과 상태 4개 검사 |
| SUPERSEDED 종료·승인 거부 | 기존 HTTP 409·SSE 종료 검사와 새 결과 문구 검사 |
| 공개 주소와 `/version` | 기존 URL 정제 및 HTML href·새 창·rel 유지 |
| 자동 감시 경고 | 대시보드·설정·운영의 기존 중복 감시 회귀 |
| project 전달·h1 | 기존 설정 뒤로 가기와 프로젝트 h1 검사, 새 모든 POST 폼 검사 |
| 설정 선택·required·저장 redirect | 기존 설정 회귀, 브라우저 onprem→cloud→Route 53 전환 |
| 승인 SHA·해시·diff·별칭 | 기존 HTTP 비교 및 새 단일 승인/거절 검사 |
| 진행 DOM·JS | 기존 node VM 계약 + 새 교차 환경 이벤트/중복 재생/종료·비대상 검사 |
| POST·CSRF·운영 폼 | 기존 CSRF 거부·unlock 회귀, 새 모든 POST 필드 검사 |
| 이스케이프 | 기존 진단·diff 이스케이프, 새 실패 error 텍스트 및 JS innerHTML 금지 반례 |
| 온프렘 기본·대상별 문구 | 기존 설정 기본값 검사, 새 온프렘 승인 범위 검사 |

대상 정보가 없는 실행은 공통 빌드 이벤트만으로 두 환경을 성공 표시하지 않는다. 명시적인 환경 이벤트를 받은 곳만 갱신한다.

기존 Jinja 블록 이름, `data-run-id/status/terminal-states/phase`, 세 이벤트 목록 ID와 `result-link`는 유지했다. 준비 모달의 기존 `data-deploy-loading`, `data-loading-message`는 인라인 안내로 남겨 두었다. `window` 없는 node VM을 지원한다. step이 있는 새 이벤트마다 `[data-phase]` 조회는 한 번이며 step 없는 이벤트는 조회하지 않는다. 같은 seq의 재연결 재생은 중복 표시하지 않는다.

## 바꾼 테스트 문자열

- `test_ui_integration_fix10.py`: 성공 전용 문구 `실행 기록 봉인 완료` 및 실패 시 부재 검사 문자열 `봉인 완료` → `승인 기록과 배포 결과를 저장했습니다.`. 성공일 때 존재, 실패·준비 실패·대체됨에는 부재라는 assert 의미를 유지했다.
- 같은 파일: 설정 뒤로 가기 링크의 `← 대시보드` → `대시보드`. 프로젝트 쿼리의 정확한 href 검사는 유지했다.
- `test_fullchain_fix8.py`: `지금 계획 요청` → `지금 배포 준비`. HTTP 상태·CSRF·요청 호출 검사는 유지했다.

## 검증

증거는 저장소 루트 `.orchestrator/design/evidence/phase2/`에 있다. 이 폴더는 Git 제외 자료이며 인계 시 함께 전달한다.

- 수정 전 웹 73개 통과: `baseline-web.log`.
- 수정 전 전체 CI: 1918 passed, 1 skipped, 4 deselected. `baseline-ci.log`.
- 교체 후 기존 웹 73개 통과: `web-after.log`.
- 최초 새 회귀 7개 통과: `redesign-tests.log`. 독립 검토 수정 후 새 회귀 13개를 포함한 웹 **86개 통과**: `review-fix-tests.log`.
- 독립 검토 수정 전 전체 CI: **1925 passed, 1 skipped, 4 deselected**, 테스트 161.61초, 전체 163.9초, exit 0. `ci-before-review.log`. 최종 수정 후 전체 검증은 아래 마감 기록에 둔다. 별도 temp-box 앱 1개는 기존 skip이다.
- 최초 전체 검사에서 새 JS 테스트 문자열의 lint와 운영 버튼 기대 문구 1건이 실패했다. 수정 후 재실행했다. `ci-before-fixes.log`에 실패도 보존한다.

```sh
UV_NO_SYNC=1 PYTHONPATH="$PWD/src" make -C harness ci
```

비밀값 검사는 아래 두 범위를 구분했다.

- `gitleaks git --redact --no-banner --no-color --log-opts='origin/o1/ui-design-base..HEAD' .`: exit 0, 새 커밋이 없으므로 **0 commits / 0 bytes**. 이것만으로 새 변경을 검사했다고 보지 않는다.
- `gitleaks stdin --redact --no-banner --no-color`: 현재 작업 diff + 신규 파일 전체를 검사해 exit 0, 발견 0. 입력은 `ui-redesign.patch`, 결과는 `gitleaks-working-tree.log`다. 파일 목록·SHA-256은 `changed-files.json`에 둔다.

실제 AWS·VM·Docker·Claude 호출 없이 기존 기본 CI 범위만 실행했다. 외부 실행 마커 4개는 제외된다. 테스트의 fake 배포는 실제 인프라 검증을 뜻하지 않는다.

## 화면 확인

`render_preview.py`는 실제 라우트/템플릿을 기존 fake 테스트 서비스로 렌더한다. 기본 승인·진행·성공·실패는 해당 fixture 서비스를 거쳤다. 인프라 경고와 추가 실패/대체 상태, 차단/준비 상태는 `source=fixture`를 명시한 렌더용 입력으로 보강했다. 목업의 가짜 63자리 해시는 사용하지 않았다.

- 주요 7화면: dashboard, approval, progress, success, failure, settings, ops.
- 크기: **1440×1000, 1280×800, 390×844**. 노트북·데스크톱 중심으로 확인.
- 추가 상태: 빈 목록, 준비 중, 차단, 온프렘 설정, 저장 알림, 종료 진행, 직접 확인 필요, 패리티 실패, 대체됨, 배포 전 실패.
- 캡처: `captures/<화면>-<폭>.jpg`. 승인 상세는 `approval-expanded-1440.jpg`.
- 전체 측정: `browser-checks.json`, **32개 화면/크기 조합**. 주요 화면의 가로 넘침 없음, 최소 글자 12px. 페이지 외곽선이나 버튼이 잘리는 곳 없음.
- 승인 실행 단계·기술 정보 펼침, 전체 SHA·해시, 3px 키보드 포커스 확인.
- onprem은 도메인 비필수, cloud는 도메인 필수다. 기존 도메인+Route 53 값을 남겼으면 onprem에서도 서버가 DNS를 검증하므로 영역 ID를 요구한다. 필요한 ID가 비어 있으면 도메인 접기를 열어 입력란을 보여 준다.
- 진행 캡처의 SSE는 테스트 서비스의 실제 기록을 재생한다. 파일 재생이 끝나면 재연결 안내가 보일 수 있다. 실제 운영 SSE 스트림은 기존 HTTP/node 테스트 범위에서 확인했으며 외부 인프라로 장시간 연결을 검증하지 않았다.

## 독립 검토에서 수정한 것

최초 사후 검토는 P2 4건으로 수정 필요 판정을 받았다.

1. 승인 IAM 메타의 `added: null` 등 자유 형식 값이 순회 오류를 만들었다. 타입을 확인하고 원문으로 대체해 렌더 실패를 막았다.
2. 실제 생성기의 `proposed_allow`를 읽지 못했다. Action·Resource를 기본 요약에 표시하고, 차분이 아닌 전체 허용 목록이라고 명시했다. 구형 `added`도 지원한다.
3. 기존 도메인+Route 53을 남기고 온프렘으로 바꾸면 필요한 영역 ID가 숨겨졌다. 서버의 실제 검사 조건과 맞추고 필요한 입력란을 펼친다. 값을 몰래 지우지 않는다.
4. 대상 정보가 null일 때 공통 빌드 성공을 두 환경에 복사했다. 대상이 알려진 경우에만 공통 빌드를 분배한다. 미확인 환경은 미확인으로 남긴다.

위 반례는 새/확장 회귀와 별도의 제한된 독립 재검토 대상으로 삼았다. 전체 검토의 다른 확인 범위와 이번 수정 범위를 구분해 기록한다.

## 수정 11과 합칠 때

시작 전에 `.worktrees/fix11`의 status와 웹 diff를 읽었다. 그 작업 트리는 수정하지 않았다. 양쪽에서 바꾼 웹 파일은 `templates/{dashboard,approval,settings,_ops}.html`과 `routes/pages.py`다. 수정 11은 `routes/{approvals,results,settings}.py`도 바꾸지만 이번 작업은 그 라우트를 수정하지 않았다. `test_ui_integration_fix10.py`와 O1 활용 기록도 겹친다.

- dashboard: 수정 11의 `setup_checklist` 구역과 `/setup` 링크를 이쪽 머리/경고 아래에 `.section`으로 옮긴다.
- settings: `/setup` 링크와 `code_patch` 체크박스를 소스 설정 구역에 붙인다. 수정 11의 입력 name·저장 처리·버전 필드는 유지한다.
- approval: 수정 11의 patch 검증·설명은 이쪽 코드 수정 구역으로 옮긴다. 승인 대상 해시와 diff를 대체하지 않는다.
- `_ops`: 초기 연결 링크를 보존하되 운영 폼의 action/CSRF/project를 유지한다.
- pages.py: 이쪽 실행 목록 표시와 수정 11의 `setup_checklist` context를 함께 남긴다.
- 수정 11의 신규 setup 화면은 이번 작업 범위 밖이다. B 토큰을 재사용하되 `.section`, 기본 체크박스, 상태 매크로로 맞춘다. 장식 카드 CSS가 사라졌으므로 기존 `card` 이름만으로 새 설정 패널이 완성된 것으로 간주하지 않는다.

문서의 합치기 순서는 디자인 변경 먼저, 수정 11을 그 위로 rebase다. 실제 merge/rebase는 하지 않았다. 공유 디렉토리 담당자에게 메시지도 보내지 않았다.

## 남은 범위와 제안

사람 대상 5초 인지성 시험, Windows/Linux 글꼴 폭, Safari/Firefox, 실제 인프라 배포는 미검증이다. 준비 요청 중에는 명시적인 ‘상태 새로고침’을 사용한다. 제품의 비동기 준비 polling을 새로 구현하지 않았다. 현재 서비스 버전은 기록만으로 확정하지 않고 `/version`으로 확인하게 한다.

제안 커밋 제목: `feat: 관리 웹에 스위스 원장 디자인 적용`

## 최종 마감 — DONE

- 최종 수정 후 전체 CI: **1931 passed, 1 skipped, 4 deselected**, 테스트 166.21초, 전체 **168.5초**, exit 0. lint/type/boundary/contracts/test 전부 PASS. 목표 90초보다는 길다. 원시 출력은 `ci.log`.
- 수정 후 웹 범위: **86 passed, 1.09초**, `review-fix-tests.log`.
- 사전 검토: REVISED. 승인·진행·환경 결과·기존 12조건을 검증 범위로 반영.
- 첫 사후 검토: P2 4건으로 수정 필요. 위 IAM 2건·DNS 1건·대상 미상 1건을 모두 수정했다.
- 별도 독립 재검토: **PASS**, 네 수정 범위에서 추가 finding 없음. 검토자가 지정 테스트 **27 passed, 0.95초**와 추가 IAM 입력 12종·JS 대상 미상 반례를 직접 실행했다. 12조건 전체를 다시 검토한 것으로 확대하지 않는다.
- 외부 Claude: 실행하지 않음. 사용자 문서의 명시적 호출 금지를 우선했다. 최종 판정은 내부 검토·재검토와 주 작업자의 전체 CI 증거에 한정한다.
- 현재 게이트: **PASS**. 발견된 네 결함은 수정 후 다시 검증했으며 나머지 원 검토 범위는 변경하지 않았다.
- `static-checks.json`: 기존 data 속성·블록 제거 0, 원격 폰트 0, 금지 장식 문자/스타일 0, template safe 필터·JS innerHTML 0. 9/10px은 상태 도형 크기이며 글자가 아니다. CSS는 147줄이다.
- 미리보기 서버는 검토 후 시작한 세션에 `quit`을 입력해 정상 종료한다. 필요하면 루트에서 `python3 .orchestrator/design/evidence/phase2/serve_preview.py`로 다시 열 수 있다. 캡처와 렌더 HTML은 보존한다.
