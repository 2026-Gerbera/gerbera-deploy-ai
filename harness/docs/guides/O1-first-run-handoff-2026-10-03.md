# 첫 실행 기본값과 설정 저장 인계 — 2026-10-03

## 작업 기준

- 작업 위치: `.worktrees/first-run`, 브랜치 `o1/first-run`.
- `git fetch --no-tags origin main`으로 확인한 기준: `4e023a86f3af797c73441d1c54fa788ac3a11c95`.
- 원래 작업 폴더의 미커밋 변경은 보존했다. 커밋·push·PR·태그는 만들지 않는다.
- 실제 AWS·VM·Docker·Claude 실행과 금지된 자격증명 경로 열람 없이 검증한다.
- 공개 선택값 자동 적용과 실제 연결·배포 성공은 별개다. 인벤토리·로그인·push 권한을 기본값으로 만들어 내지 않는다.

## 수정 전 근거

`UV_NO_SYNC=1 PYTHONPATH=<wt>/src make -C harness ci`

- 종료 코드 0. lint/type/boundary/contracts PASS.
- `3634 passed, 1 skipped, 4 deselected`, 실패 0. pytest 268.89초, 전체 271.8초.
- 원문: `harness/var/validation/first-run/baseline-ci.log`.
- 데스크톱의 실제 DB·프로세스 환경은 이 작업에서 직접 확인하지 않았다. 동일 조건의 재현과 리허설 당시 단일 원인 확정은 구별한다.

## 409 원인별 재현

기준 코드의 `/setup/choices`는 `SetupService.save_choices`에서 나온 `DdakToolError`를 모두 같은 409 문구로 바꾼다. 따라서 화면의 문구만으로 아래 원인을 구별할 수 없었다.

| 후보 | 기준 코드 확인 | 수정 전 재현 |
|---|---|---|
| 중복 자동 감시 | Store가 정규화된 저장소·브랜치가 같은 다른 프로젝트를 발견하면 CONFIG_INVALID | 구버전 DB에 두 감시 주체를 넣고 연결 선택만 저장해도 실패 |
| 설정 버전 | 수정 필드가 겹치지 않아도 버전이 다르면 PRECONDITION_FAILED | `/setup`에서 이미지 저장소 변경 뒤, 오래된 `/settings` 폼으로 브랜치 변경 시 실패 |
| 로컬 CLI 판정 | `INVOCATION_ID`를 원격 표식으로 취급 | WSL·systemd 표식과 loopback 주소에서 Claude 역할 저장 실패 |
| 입력·provider 검증 | 허용 필드와 provider 역할 검사는 별도 유지해야 하는 조건 | 추가 회귀에서 정상 조합과 잘못된 입력을 구별 |

첫 세 사례의 명령: `UV_NO_SYNC=1 PYTHONPATH=<wt>/src make -C harness test ARGS='tests/unit/test_first_run.py'`.
수정 전 **3 failed**, make 종료 코드 2이며 import·fixture 실패가 아니라 위 제품 예외로 실패했다.
원문은 `harness/var/validation/first-run/red-core.log`다.

웹 회귀의 첫 실행은 `test_first_run_web.py`에서 **6 failed, 3 passed**였다.
저장부 수정이 시작된 뒤 웹 수정 전의 결과로, 400·409·502에서 HTML 폼 유지가 없고 감시 전환 알림이 빠진 것을 확인했다. 원문은 `red-web.log`다.

## 적용 동작

- `harness/config/defaults.toml`을 cwd와 무관하게 읽는다. REAL 첫 실행의 생성·판단은 `claude-cli` / `claude-sonnet-5-5`, 추론 `low`, 제한 90초, 빌드 `local`, 이미지 `2026gerbera/flaskr`, 감시 `prod`다. 빌더 이름을 지정하지 않으면 기존 제품 전용 `ddak-product-*` 규칙을 쓴다. FAKE/REPLAY 테스트 모드는 유지한다.
- 관리 페이지의 명시값이 실행환경과 기본 파일보다 우선한다. `/setup` 입력에는 최종 적용값과 `기본 파일`·`관리 페이지`·`머신 git 신원` 등의 출처를 표시한다. 저장소 URL이 없으면 자동 감시는 시작하지 않는다.
- 작성자 이름·이메일은 앱 checkout의 로컬 Git 설정, 머신 전역 설정 순으로 읽는다. 명시한 관리값은 우선하며 봇 신원은 거부한다. 누락 시 `설정 필요`로 준비를 막는다. Git 설정을 쓰거나 임의 신원을 만들지 않는다.
- 설정 저장은 SQLite의 한 트랜잭션에서 검증·병합·감시 소유권 이관을 수행한다. 동일한 정규화 저장소·브랜치의 다른 프로젝트는 `auto_detect=false`가 되고 버전이 증가한다. 저장한 프로젝트가 새 감시 주체라는 알림을 `/setup`과 `/settings`에 표시한다.
- 버전별 원본과 서버가 표시한 공개 기본값을 저장한다. 폼에는 프로젝트·버전·표시값에 묶인 식별자를 넣어, 같은 버전이라도 재시작 후 환경값이 바뀐 화면을 구별한다. 서로 다른 표시 snapshot은 프로젝트당 최근 128개를 보관하며 만료·변조된 참조는 재로딩을 요구한다. 제출 중 실제 변경한 필드만 병합하므로 오래된 폼의 미변경 prefill이 최신 값을 덮지 않는다. 같은 필드의 서로 다른 관리값 수정·기본값 복귀는 충돌로 보고, 같은 관리값 재제출은 허용한다. 실패 시 감시 이관도 롤백한다.
- 감시 콜백은 소유권을 다시 확인하고 저장소·브랜치별로 직렬화한다. 이관 중 남은 콜백과 재시작 뒤 활성 자동 run의 같은 SHA를 중복 준비하지 않는다. 취소 후에도 살아 있는 계획 스레드와 소스 복사 스레드는 종료까지 기다린 뒤 새 감시 주체를 시작한다. 실패한 준비는 새 소유자가 재시도할 수 있다.
- `INVOCATION_ID`만으로 로컬 셸을 원격으로 판정하지 않는다. SSH, CI, 컨테이너, loopback 이외 바인딩 차단은 유지한다.
- `/setup`의 400·409·502는 해당 폼 아래 오류 상자로 반환한다. 일반 입력은 유지하고 비밀 입력은 비운다. 인벤토리는 전송값을 복사한 뒤 브라우저에서 비밀값 형태만 가리고 공개 입력은 유지한다. 오류 응답이 도착하면 서버 마스킹 결과로 다시 채운다. 응답 유실에도 공개 JSON을 보존하며, 작은따옴표·닫히지 않은 비밀 문자열도 가린다. 브라우저 fetch 경로는 실패 시 URL을 유지하며, JavaScript가 없는 경우에도 HTML 폼을 반환한다. CSRF·Origin·Host 검사는 유지한다.
- `/settings`도 동일 기본값 해석과 출처를 사용한다. 저장소만 있던 예전 설정에서 실제 감시는 켜져 있는데 화면 체크가 꺼져 보이던 불일치를 수정했다.
- 최초 저장소 URL 입력과 함께 제출한 자동 감지 OFF는 명시적으로 보존한다. 저장소가 생기며 TOML 기본값의 조건이 바뀌더라도 OFF를 삭제하거나 기존 감시 주체를 빼앗지 않는다.

## 검증 기록

- `gitleaks git --redact --report-format json --report-path harness/var/validation/first-run/gitleaks-git.json .`: 종료 코드 0, 212개 커밋, 발견 0건. 원문 `gitleaks-git.log`, 보고서 `gitleaks-git.json`.
- 추가 회귀 71 passed, 웹 호환 보완 55 passed. 각 원문은 `testauthor-final.log`, `web-compatibility.log`다.
- 기존 stale 병합·관리값 우선 테스트 보완 후 87 passed(`merge-precedence-compatibility.log`), 로컬 Git fixture E2E 11 passed / 56.03초(`e2e-compatibility.log`).
- 중간 전체 `check`에서 16 failed / 7 errors를 확인했다(`backend-check.log`). 구버전의 환경값 우선·모든 stale 저장 거부 기대, 템플릿 문맥 기본값 누락, 초기화 생략 테스트 서비스, 로컬 Git fixture의 이전 monkeypatch 위치를 수정했다. 생산 HTTPS 저장소 검증은 유지했다. 최종 CI 결과는 아래에 별도 기록한다.
- 브라우저는 loopback의 가짜 서비스로 검사했다. 이미지 저장소 입력을 변경한 뒤 409를 받았을 때 URL과 입력값이 유지됐다. 가짜 비밀값의 저장 실패 400 뒤 password 입력이 비어 있었다. 실제 연결·배포 호출은 없었다. `browser-errors.jpg`와 `ui_preview.py`를 보존했고 임시 서버는 정상 종료했다.
- 독립 사후 검토에서 계획 스레드 취소 후 중첩, 환경값 복귀 충돌 누락, 같은 버전의 재시작 prefill 오인, 인벤토리 textarea 비밀값 잔존을 확인했다. 이를 수정하고 RED 재현을 추가했다(`red-threads.log` 2 failed, `review-merge-red.log` 3 failed). 스레드 준비 경로 19 passed, 저장·웹 묶음 174 passed, 최종 신규 회귀 묶음 47 passed다. 인벤토리 400·409·502·응답 유실은 실제 템플릿 JS를 Node DOM 대역으로 실행했다.
- 보완 후 실제 브라우저에서도 인벤토리 저장 실패 400 뒤 URL 유지, 공개 `mode` 유지와 비밀번호 `[REDACTED]` 처리를 확인했다. 화면은 `browser-inventory-error.jpg`다. 가짜 서비스만 사용했고 임시 서버·탭을 정상 종료했다.
- 첫 전체 CI는 3672 passed / 0 failed / 1 skipped / 4 deselected였지만 검사 중 추가 회귀가 작성되어 최종 증거로 사용하지 않는다(`ci-before-review.log`, 입력 해시 일치 false). 이후 전체 결과는 `ci-final.log`와 검사 전후 코드·테스트 해시로 확인한다.
- 후속 독립 검토에서 최초 URL 저장의 OFF 제출이 기본 ON으로 바뀌는 문제와 네트워크 오류의 공개 인벤토리 유실을 추가 확인했다. 두 OFF 사례와 네트워크 사례의 RED 3건을 남기고 수정했다(`review-second-red.log`). 닫히지 않은 문자열·작은따옴표의 서버/브라우저 가림도 보완했다(`inventory-malformed-red.log`). 공개 원문·중첩 비밀키·자격증명 URL·깨진 JSON을 HTTP 400/409/502와 네트워크 예외로 검사한 28개 JS 사례를 포함해 관련 166개가 통과했다(`review-second-final-green.log`).
- 마지막 마스킹 재검토의 빈 사용자명 URL과 역슬래시로 끝난 미완성 문자열도 RED 8건으로 재현하고 수정했다(`inventory-edge-red.log`). JS 36사례와 기존 공통 마스킹·웹·첫 실행 회귀를 합쳐 227 passed(`review-edge-green.log`). 공통 URL 마스킹은 빈 사용자명도 비밀번호를 가리고, 미종결 문자열은 마지막 escape까지 가린다.
- 독립 집중 재검토 **PASS**. 검토자가 마지막 두 사례와 직접 회귀인 JS 36개·기존 redact 53개를 별도로 실행해 **89 passed**, skip 없음으로 확인했다. 이전 검토에서 확정한 감시·병합·화면·입력 보존 문제의 보완을 모두 반영했다. 최종 집중 검토 원문은 `review-final.txt`이며 실제 인프라 실행·브라우저 전체 경로 검사와 구별한다.

### 최종 전체 검사

- 명령: `UV_NO_SYNC=1 PYTHONPATH=<wt>/src make -C harness ci` (`<wt>`는 `.worktrees/first-run`의 절대 경로).
- 종료 코드 **0**, **3719 passed / 0 failed / 1 skipped / 4 deselected**, pytest 223.96초, 전체 226.704초.
- lint·type·boundary·contracts PASS. 기존 격리 temp-box 프로젝트 전용 검사 1개는 skip, 실제 Docker·AWS·LLM marker 4개는 제외됐다. 요구된 비실서비스 CI 실패는 없다.
- 시작 `2026-10-03T10:44:14.143204+00:00`, 완료 `2026-10-03T10:48:00.845849+00:00`. 코드·템플릿·테스트·기본 설정 등 446개 입력 파일의 검사 전후 SHA-256이 일치한다.
- 원문·정확한 명령·해시: `ci-final.log`, `ci-final-summary.json`, `ci-input-sha256.json`.
- 최종 `gitleaks git --redact --report-format json --report-path harness/var/validation/first-run/gitleaks-git-final.json .`: 종료 코드 **0**, 214 commits, 발견 **0건**. 앞선 212개 결과와 구별하며 Git 이력 검사 대상 참조의 증가가 작업 브랜치 커밋을 의미하지 않는다. 작업 HEAD는 기준 `4e023a86f3af797c73441d1c54fa788ac3a11c95` 그대로다.
- 미커밋 변경·신규 31파일은 `.snapshot.txt` 사본으로 범위를 제한해 `gitleaks dir --redact --report-format json --report-path harness/var/validation/first-run/gitleaks-changes.json harness/var/validation/first-run/changed-scan`으로 검사했다. 종료 코드 **0**, 발견 **0건**. 검사 결과·정확한 파일과 해시는 `gitleaks-changes.log`, `gitleaks-changes.json`, `changed-scan-manifest.json`에 남긴다.

## 완료 상태

**DONE**. 요청한 구현·재현·회귀·전체 CI·비밀 검사·기록을 완료했다. 작업 HEAD와 index를 바꾸지 않았다. 최종 근거는 `ci-final*`, `gitleaks-final-summary.json`, `changed-scan-manifest.json`, `review-final.txt`다. 이전 `ci-before-*`는 중간 증거이며 최종 코드의 통과 근거로 섞지 않는다.

## 범위와 인계 주의

- 리허설의 실제 데스크톱 DB·환경을 읽지 않았으므로 당시 원인을 하나로 단정하지 않는다. 중복 자동 감시, 겹치지 않는 설정의 버전 차이, 로컬 systemd 표식이라는 세 원인은 각각 재현·제거했다. 올바른 Claude/Claude 선택은 통과하며 잘못된 필드·provider는 계속 거부한다.
- 기본 공개 설정과 머신 Git 신원 적용을 검증한 것이다. 기존 CLI 로그인, Docker/VM, 인벤토리, 저장소 push 권한까지 새로 생성하거나 실서비스 배포 성공을 확인한 것은 아니다.
- 증거는 `harness/var/validation/first-run/`에 보관한다. 테스트 원본 파일명 `test_*.py` 그대로의 증거 사본은 만들지 않는다.
- `.venv`는 검증에 사용한 기존 루트 환경을 가리키는 미추적 링크다. 환경 내용은 바꾸지 않았으며 변경 파일 목록에 포함하지 않는다. stage·커밋·push·PR·태그 없이 작업 파일만 남긴다.
- 외부 Claude 검토는 사용자 호출 금지 지시에 따라 수행하지 않는다. 독립 내부 검토와 재현·회귀 결과로 판단한다.
