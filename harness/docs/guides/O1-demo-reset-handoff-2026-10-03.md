# 관리 페이지 시연 초기화

- 작업: `.worktrees/demo-reset`, `o1/demo-reset`, 기준 `origin/main` d01b7d4.
- 운영 화면의 **시연 초기화**에서 **v1으로 되돌리기** / **v2 시연 PR 준비**를 제공한다.
- GitHub 앱 저장소의 `prod`, `v1`, `v2`가 필요하다. 프로젝트 설정의 `prod` 자동 감시를 켠다. 대상은 저장된 `default_targets`를 따른다.
- 사람은 생성된 PR 링크에서 merge하고, 연결된 승인 화면에서 승인한다. PR merge 자체는 배포 완료가 아니다.
- prod가 이미 v1이면 PR을 만들지 않고 prod 재배포 준비를 요청한다. 실제 서버가 v2인 경우도 기존 승인·빌드·배포 경로로 복구한다.
- v1 배포 완료는 선택 환경 모두의 성공 장부, 원본 커밋의 v1 트리, 실제 `/version.release_id` 일치를 확인한 뒤 표시한다. 한쪽만 성공하면 전체 완료로 표시하지 않는다. 온프렘 단독은 클라우드를 기다리지 않는다.
- v1 배포 완료 뒤 v2 PR을 준비한다. 승인 대기 중 다음 PR은 merge하지 않는다.

## 변경 파일

- `src/ddak/core/demo_cycle.py`: 기존 스크립트의 트리·부모 검증, 새 demo 브랜치 push, PR 설명을 공용화.
- `harness/scripts/demo_cycle.py`: 공용 함수 사용. 기존 CLI 인터페이스 유지.
- `src/ddak/core/demo_backend.py`: Git/GitHub 어댑터, 공개 버전 응답 조회. 관리 push 토큰 우선, 없을 때 머신 Git credential helper. 토큰은 HTTP 인증 헤더에만 사용하며 argv·기록에 저장하지 않는다.
- `src/ddak/core/demo_reset.py`: 공개 PR 기록 보존, 현재 서비스 버전 및 시연 진행 상태 조합.
- `src/ddak/app.py`: 서비스·인벤토리 공개 주소 조립. FAKE 모드 실제 Git/GitHub 조회·변경 차단.
- `src/ddak/web/routes/ops.py`, `templates/ops.html`, `templates/_demo_reset.html`, `templates/_demo_state.html`, `static/demo_reset.js`: 운영 버튼, 기존 CSRF·폼 오류 경로, 15초 상태 조회, 승인·PR·버전 링크.
- `harness/tests/unit/test_demo_reset.py`: 메모리 저장소·가짜 GitHub·가짜 버전 응답 회귀.

## 확인한 것

관련 pytest 3파일: `test_demo_reset.py`, `test_ui_integration_fix10.py`, `test_form_errors.py`.
**50 passed / 0 failed (1.17초)**. 수정 Python 파일 ruff check 및 git diff --check 통과.
신규 13개 회귀에는 v1→v2 PR 트리/부모, PR 재사용, prod·태그 원격 변경 금지, 대상별 완료 판정, 서버의 이전 release_id, prod만 v1인 경우 재배포 요청, 승인 화면 링크, CSRF/폼 오류, 자격 증명 우선순위, FAKE 차단, CLI 공용 함수 연결이 포함된다.
실제 GitHub·AWS·VM·Docker·AI 호출이나 원격 쓰기는 하지 않았다. 전체 CI와 gitleaks는 속도 우선 지시에 따라 생략했다.

## 기록 위치

`<제품 var>/demo-reset/<project>.json`: PR 번호·링크·태그·기준 트리·대상·시각(비밀값 없음).
`<제품 var>/demo-reset/repositories/<project>/`: 제품 전용 앱 checkout.
배포 기록은 기존 `runs/<run_id>`와 환경별 장부를 계속 쓴다.

## 후속 작업

- 실제 데스크톱에서 PR 생성 권한(관리 push 토큰 또는 머신 helper), merge 자동 감시, 양쪽 `/version` 응답을 실측한다. 이 작업은 가짜 검증까지만 수행했다.
- PR 생성 API가 실패한 직후의 고아 demo 브랜치 재사용·정리와 컨트롤러 정지 중 merge의 추적 재시도는 후속 보강 대상이다.
- v1/v2 태그 트리 외 변경이 추가된 경우에는 ‘v1·v2 아님’으로 표시한다. 임의로 v1 완료로 판정하지 않는다.
- 미병합 `e2e-ux1`의 Docker 로그인·빈 오류 박스 수정은 이 main 기준 worktree에 포함하지 않았다. 통합 시 해당 변경을 별도로 반영한다.
