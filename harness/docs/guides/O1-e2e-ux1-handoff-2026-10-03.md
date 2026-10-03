# 데스크톱 승인 화면·빈 오류 표시 수정

- 작업 위치: `.worktrees/e2e-ux1`, 브랜치 `o1/e2e-ux1`, 기준 `origin/main` d01b7d4.
- `src/ddak/web/templates/approval.html`: 승인·거절 폼을 상단 배포 소스·위치 요약 바로 아래로 이동했다. 계획 판정 근거는 포함·제외 단계 수, 환경 키 총수·비밀/일반 개수, 판정 제공자·모델의 표로 표시한다. 전체 JSON은 기본 접힌 기술 정보에 보존한다. 승인 해시·POST 동작은 변경하지 않았다.
- `src/ddak/web/templates/_status.html`, `base.html`: 오류가 없으면 오류 상자를 렌더링하지 않는다. JS용 원형만 비활성 `<template>` 안에 남기며 실제 오류 시 기존 JS가 복제해 폼 아래에 표시한다.
- `harness/tests/unit/test_e2e_ux1.py`: 긴 판정 근거에서도 버튼 선행·JSON 접힘·집계 정확성, 승인/결과/ops의 빈 오류 제거, 실제 오류 유지, 승인 별칭 동일 HTML을 확인한다.

## 관련 검증

명령: `PYTHONPATH=<worktree>/src <기존 .venv>/bin/python -m pytest harness/tests/unit/test_e2e_ux1.py harness/tests/unit/test_ui_integration_fix10.py harness/tests/unit/test_ui_redesign.py harness/tests/unit/test_form_errors.py harness/tests/unit/test_form_submit_js.py -q`

결과: **71 passed, 0 failed, 2.24초**. 기존 승인 동선·별칭·window 없는 Node VM·step 조회 횟수·폼 오류 회귀 포함. 전체 CI·gitleaks·독립 검토는 사용자 속도 우선 지시로 실행하지 않았다. 커밋·push·PR은 하지 않았다.

## 후속 작업

- 데스크톱 브라우저에서 실제 창 크기로 상단 승인 버튼과 빈 오류 박스 미표시를 확인한다. 추가 코드 수정 항목 없음.


## 보충: Docker Hub 로그인·저장소 검사

- 변경 파일: `core/docker_auth.py`(공통 인증 항목 판정), `core/setup_tools.py`(로그인 실패 분류·저장 결과 확인·저장소 읽기), `core/setup_service.py`(로그인 직후 검사·점검표 기록), `cloud/build/local.py`(승인 전 인증 검사), `web/templates/setup.html`(필수 사용자명과 네임스페이스 기본값), `web/static/app.js`(실패 시 현재 화면의 점검표 빨강 표시).
- 사용자명은 이미지 저장소 네임스페이스로 미리 채우고 수정할 수 있다. 조직 저장소는 토큰을 발급한 계정명으로 바꿀 수 있게 안내한다. 빈 사용자명은 서버도 거부한다.
- 로그인 명령이 성공해도 제품 설정에 제출한 인증이 저장되지 않았으면 오류다. 사용자명 누락·인증 실패·네트워크 실패는 고정된 문구로 분리하며 토큰과 프로세스 원문은 내보내지 않는다.
- 로그인 직후 Docker Hub의 pull 범위 토큰을 얻고 Registry V2의 `tags/list?n=1` 읽기를 검사한다. latest 태그나 이미지 다운로드를 요구하지 않는다. 인증 정보는 HTTP 헤더에만 사용하며 외부 호스트 리다이렉트는 거부한다. 읽기 성공만 녹색, 실패는 빨강 기록과 폼 오류로 남는다. push 권한까지 검증했다고 표시하지 않는다.
- `auths` 안의 빈 객체는 인증으로 인정하지 않는다. Hub 항목에 비어 있지 않은 `auth` 또는 `identitytoken`이 있어야 로컬 빌드 사전 검사를 통과한다. 없으면 승인 전에 **설정 필요: Docker Hub 로그인**으로 중단한다.
- 관련 검증: `test_docker_login_ux1.py`, `test_setup_tools_fix11.py`, `test_setup_review_fix11.py`, `test_setup_service_fix11.py`, `test_setup_web_fix11.py`, `test_form_submit_js.py` → **189 passed / 실패 0 / 2.99초**. 현재 화면의 Docker 점검표 빨강 전환 검증을 추가한 뒤 `test_form_submit_js.py` → **16 passed / 실패 0 / 0.99초**.
- 테스트는 가짜 Docker runner·가짜 HTTP 응답과 임시 설정만 사용했다. 실제 Docker·네트워크 인증·비밀 파일은 사용하지 않았다. 전체 CI·gitleaks·독립 검토는 생략했다.

### 보충 후속 작업

- 데스크톱에서 계정명을 확인하고 다시 로그인해 저장소 읽기 녹색 전환을 확인한다. 실제 push 권한은 이후 빌드 실행에서 확인한다.
- 아직 원격에 존재하지 않는 새 저장소는 읽기 검사에서 실패한다. 새 저장소 첫 생성 흐름은 별도 후속 범위다.
