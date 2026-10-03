# 관리 폼 오류 표시 — 2026-10-03

## 변경 내용

기준은 `origin/main`의 `1d3bb91`, 작업 위치는 `.worktrees/setup-error`, 브랜치는 `o1/setup-error`다. 처음의 `/setup` 한정 수정에서 사용자 추가 지시에 따라 관리 웹의 모든 POST 폼으로 범위를 넓혔다.

- JavaScript는 폼 제출을 fetch로 처리한다. 실패하면 페이지를 이동하지 않고 제출한 폼 바로 아래에 `⚠ 오류`, 코드, 문구를 표시한다. 입력값은 브라우저에 그대로 두며 중복 제출을 막았다가 실패 시 버튼을 다시 활성화한다.
- 성공하면 기존 이동 주소를 따르거나 현재 화면을 갱신한다. 승인·거절 버튼의 `decision` 값도 그대로 보낸다.
- 서버는 fetch 요청에 `{error: {code, message}}`를 실패 HTTP 상태로 반환한다. 코드에서 만든 `DdakToolError`는 문구와 코드를 유지해 redact하고, 일반 SDK·프로세스 예외는 고정 문구로 숨긴다. 기존 API 클라이언트의 실패 HTTP 상태도 유지한다.
- 버전 충돌만 **다른 화면에서 설정이 바뀌었습니다. 새로고침 후 다시 저장하세요**로 표시한다. 버전 검사를 우회하거나 자동 재저장하지 않는다.
- JS 없는 브라우저 요청은 303으로 원래 GET 화면에 돌아온다. 오류는 해당 폼 밑에 한 번 표시한다. 공개 위치 메타는 크기·형식 검사 실패에도 보존하지만 승인·입력 검증에는 사용하지 않는다.
- JS 없는 복귀에서는 비밀 입력이나 제출값을 서버에 보관해 다시 채우지 않는다. 실패 중 승인 화면이 닫혀 결과 화면으로 이동하면 오류를 결과 화면에 보존한다. 준비 작업의 승인 버튼이 사라져도 해당 작업 요약 안의 오류 상자는 유지한다.

## 구조와 보호 조건

`web/form_errors.py`의 공통 `FormRoute`를 setup·setup_actions·settings·approvals·ops 라우터에 적용했다. pages·results도 복귀 시 HTML 오류 표시를 지원한다. `forms.py`는 UI 위치 메타를 업무 필드와 분리해 기존 엄격한 입력 필드 검사를 유지한다.

오류 전달은 프로세스 메모리의 최대 128건·5분 일회성 기록을 쓴다. 303 URL에는 불투명 식별자만 넣고, 기록은 CSRF 세션·돌아갈 화면·프로젝트에 묶는다. 서로 다른 탭의 오류를 덮어쓰지 않는다. 내용은 소비 또는 만료 시 지우며 재시작 후에는 유지하지 않는다. 제출 입력은 저장하지 않고 이미 가린 오류만 저장한다.

`value`·`token` 입력을 오류 문구에서 제거한 뒤 공통 redact를 적용한다. 화면은 HTML escaping, JS는 textContent를 사용하며 비밀 입력·SDK 원문을 HTML/응답/flash에 싣지 않는다. 응답은 `Cache-Control: no-store`다. Host·Origin·CSRF 검사는 기존 `require_safe_post`를 그대로 사용한다. 돌아갈 주소는 제품 내부 GET 허용 목록으로 제한한다.

B안 디자인의 `_status.html` 오류 매크로와 새 `_form_errors.html`을 쓴다. 대상 템플릿은 setup·setup_actions·settings·approval·dashboard·_ops이며 키·토큰·환경·도구 설치·배포 요청·승인/거절·잠금 해제 폼을 포함한다. 페이지 렌더링 자체도 실패하면 JSON 대신 안전한 `form_failure.html`과 새로고침 링크를 보인다.

`app.js`의 window 없는 Node VM 실행, step 이벤트당 `[data-phase]` 정확히 한 번 조회, `/ops` 승인 별칭과 정식 승인 화면의 동일 HTML을 유지한다.

## Claude CLI 판단 제공자

실제 `SetupService`·Store·앱 설정 조립을 거치는 가짜 로컬 환경에서 **생성 Claude API + 판단 Claude CLI + 커밋 작성자 이름·이메일** 저장과 재표시가 통과했다. 정상 조합을 차단하는 결함은 재현되지 않아 제공자 검증을 바꾸지 않았다.

loopback이 아닌 호스트는 기존대로 거부하고 이제 오류 코드와 이유가 보인다. 실제 데스크톱의 기존 저장 실패가 버전 충돌·CLI 실행 환경·감시 중복 중 무엇 때문인지는 확정하지 않았다. 업데이트 후 같은 저장 동작의 오류 상자로 확인해야 한다. 연결 검사 함수가 빨간 상태를 정상 반환하는 기존 경로는 상태 배지를 유지한다.

## 확인한 것

최종 검증 결과는 아래에 기록한다. 로그는 `harness/var/validation/setup-error-20261003/`에 보관한다. 테스트 소스는 이 경로에 복사하지 않는다.

- 집중 검증: 대표 폼, JS/no-JS, 입력 보존, 승인/거절, CSRF, 별칭, B안·진행 화면 회귀 249 passed. 후속 크기/형식·준비 실패 위치·최소 요청 객체 회귀를 포함한 관련 62 passed.
- 최초 확장 전체 CI: 3685 passed / 2 failed / 1 skipped / 4 deselected. 최소 Request fixture의 query_string 생략을 공통 오류 표시 코드가 처리하지 못한 2건을 수정했다. `ci-forms.log`에 보존한다.
- 최종 전체 CI: **3691 passed / 0 failed / 1 skipped / 4 deselected**, pytest 239.43초, 전체 242.438초, exit 0. 완료 시각 2026-10-03 18:10:22 KST. lint·type·boundary·contracts·test 모두 PASS. skip은 별도 temp-box 앱 프로젝트 테스트이며 외부 실행 마커 4개는 제외됐다.
- `gitleaks git --redact --no-banner .`: 212 commits / no leaks found / exit 0 / 0.868초.
- 미커밋 변경 파일 29개의 `gitleaks stdin --redact --no-banner`: no leaks found / exit 0. 이력 검사는 미커밋 코드를 포함하지 않으므로 별도로 검사했다.

명령은 `UV_NO_SYNC=1 PYTHONPATH=<worktree>/src make -C harness ci`이며 기존 설치 의존성만 쓰도록 `DEV=<저장소>/.venv/bin/python scripts/dev.py`를 지정했다. 검증용 `.venv` 심볼릭 링크는 제거했으며 공유 의존성은 변경하지 않았다. 초기 `/setup` 한정 3653개 결과는 `ci.log`의 이전 중간 검증이고 최종 확장 범위의 검증은 `ci-forms-final.log`다.

실제 AWS·VM·Docker·Claude 호출은 없으며 외부 환경의 동작은 주장하지 않는다. 커밋·push·PR은 사용자가 진행한다.

## 검토와 인계

독립 검토에서 큰 폼의 원래 프로젝트 유실, 준비 작업 실패 후 폼이 사라질 때 오류 위치 유실을 지적했다. 공개 위치 메타 보존과 작업 요약 내부 오류 매크로로 수정했고 두 경우를 회귀로 확인했다. 두 지적은 후속 독립 정적 검토에서 해결 확인됐다. 검토자는 테스트를 별도로 실행하지 않았으며 위 통과 수는 이 작업의 실제 실행 결과다. 외부 Claude는 사용자 금지에 따라 호출하지 않았다. 사전 REVISED / 사후 REVISED → 지적 해결 확인 / 외부 NOT_REQUIRED이며 남은 차단 항목은 없다.

서윤님께 공유할 파일: 공통 `form_errors.py`·`forms.py`·`dependencies.py`, 해당 라우터, `app.js`, 오류 매크로와 폼 템플릿. 새 POST 폼은 `FormRoute` 라우터를 사용하고 `form_fields(id)`와 폼 직후 `form_error(id)`를 함께 넣는다. 업무 필드와 UI 메타는 공통 파서가 분리한다. 준비 작업처럼 완료 후 폼이 사라지는 화면은 오류 매크로를 상태 조건 밖에 둔다.

제안 커밋 메시지: `fix: 관리 폼 오류를 현재 화면에 표시`

## PR 본문 초안

### 변경 내용

설정·배포·승인·잠금 해제 등 관리 폼 오류를 제출한 화면에 표시한다. 통제된 오류는 코드와 가린 문구를 제공하고 외부 예외 원문은 숨긴다. JS 미사용 시에도 원래 화면에 303으로 복귀해 해당 폼에 알림을 표시한다.

### 확인한 것

가짜 서비스와 TestClient·Node VM으로 정상 저장, 버전 충돌, 제공자 검증, 비밀값 비노출, 동일 화면 유지와 no-JS 복귀를 검증했다. 전체 CI와 비밀값 검사 수치는 위 최종 검증 절을 따른다.
