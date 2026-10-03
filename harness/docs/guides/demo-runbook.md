# 시연 반복 대본 — 온프렘 v1 → v2

10/3 기준. 데스크탑 WSL 온프렘 풀체인이 성공한 흐름이 바탕이다. v1 첫 배포는 `run-20261002-175023-84a6`, v2 PR merge 자동 감지와 WAS만 재빌드는 `run-20261002-180550-2d12`다. 클라우드 순서는 따로 정해지면 덧붙인다. 관리 웹 기동·환경변수·VM 준비는 [온프렘 풀체인 런북](onprem-fullchain-runbook.md)을 따른다.

## 0. 무엇을 반복하나

- **시연 장면:** GitHub에서 v2 PR을 merge한다. 제품이 prod 변경을 감지하고 계획을 세운다. 승인 1회 뒤 WAS만 다시 빌드·배포한다. 공개 URL에서 v2 박스를 보여 준다.
- **반복하려면** prod를 v1 내용으로 되돌리고 새 v2 PR을 열어 둬야 한다. `harness/scripts/demo_cycle.py`가 이 두 PR을 만든다. **merge는 사람이 GitHub에서 한다.** 스크립트는 merge하지 않는다.
- 앱 저장소는 `2026-Gerbera/gerbera-application`이다. 태그는 v1=04d779d, v2=adcafb9(feature/image-box)다.

| 명령 | 하는 일 |
|---|---|
| `make -C harness demo-status` | prod HEAD 내용이 v1·v2 중 무엇과 같은지, 열린 `demo/*` PR이 지금 merge해도 되는지 보여 준다. 조회만 한다. |
| `make -C harness demo-reset-v1` | prod를 부모로 하고 태그 v1의 트리를 그대로 쓰는 커밋 1개를 만든다. `demo/reset-v1-<시각>` 브랜치를 push하고 prod 대상 PR을 연다. PR URL을 출력한다. |
| `make -C harness demo-prepare-v2` | 같은 방식으로 태그 v2의 트리를 쓰는 `demo/v2-<시각>` PR을 연다. 시연 때 merge할 PR이다. |

- `DRY=1`을 붙이면 clone·조회·로컬 계산만 한다. push와 PR 생성은 하지 않는다. 예: `make -C harness demo-reset-v1 DRY=1`
- 다른 저장소는 `REPO=OWNER/NAME`으로 지정한다.
- 직접 실행도 된다: `uv run python scripts/demo_cycle.py {status|reset-v1|prepare-v2} [--dry-run] [--repo ...]` (harness에서)
- `make help`는 이름에 숫자가 있는 타깃을 목록에 보여 주지 않는다. `demo-status` 설명에 두 타깃 이름을 적어 두었다.
- 기존 `make -C harness demo-reset`(O1 fixture 상태 초기화)과 다른 명령이다. 시연 반복에는 `demo-reset-v1`을 쓴다.

## 1. 준비물(한 번)

- `gh auth status`로 GitHub CLI 로그인을 확인한다. 앱 저장소에 브랜치 push와 PR 생성 권한이 있어야 한다.
- git 사용자 이름·이메일이 본인 것으로 전역(`git config --global user.name`) 설정돼 있어야 한다. 스크립트는 임시 clone에서 커밋하므로 다른 저장소의 로컬 설정은 쓰이지 않는다. 스크립트는 git 설정을 바꾸지 않는다.
- 앱 저장소 clone·push에 git이 GitHub 인증을 쓸 수 있어야 한다. `gh auth status`의 Git operations protocol과 자격증명 도우미를 확인한다.
- `make -C harness onprem-run`으로 관리 웹과 감시를 띄워 둔다. 기본 접속은 `http://127.0.0.1:8765/ops`다. **컨트롤러를 먼저 띄운 뒤 merge한다.** 감시가 뜨기 전에 merge한 커밋은 기준선으로만 저장된다.
- 관리 페이지 설정에서 앱 URL, prod 감시, onprem 대상, 자동 감시 ON을 확인한다. 같은 저장소를 감시하는 프로젝트는 하나만 켠다.

## 2. 시연 전 — v1으로 되돌리고 v2 PR 준비

리허설 시간에 미리 끝낸다. v1 배포 시간이 들기 때문에 시연 직전에 하지 않는다.

1. **상태 확인:** `make -C harness demo-status`
   - `prod HEAD ... 트리=v1`이면 4번으로 간다. `트리=v2`면 2번으로 간다.
   - `v1·v2 아님`이면 prod에 다른 변경이 들어간 것이다. 2번으로 v1부터 맞춘다.
   - 관리 페이지에 승인 대기(AWAITING_APPROVAL) run이 없는지 본다. 있으면 먼저 승인하거나 거절한다.
2. **되돌림 PR:** `make -C harness demo-reset-v1 DRY=1`로 미리 보고 `make -C harness demo-reset-v1`을 실행한다. 출력된 PR URL을 열어 **GitHub에서 merge한다.** merge 방식은 상관없다. 이 PR의 부모가 현재 prod라서 merge 결과가 v1 트리와 같다.
3. **관리 페이지 승인:** 감시가 새 prod 커밋을 감지한다. 분석·계획과 승인 전 소스·gitleaks·빌더 검사가 끝나면 승인 대기가 뜬다. 원본 SHA를 확인하고 한 번 승인한다.
4. **v1 확인:** 결과가 SUCCEEDED인지 본다. 결과 화면의 공개 URL을 연다. 목록 페이지에 이미지·박스가 없어야 한다(v1의 `<svg>` 0개). `/version`의 release_id가 이 run_id인지 본다.
5. **v2 PR 준비:** `make -C harness demo-prepare-v2`. PR URL을 브라우저 탭에 열어 두고 **merge하지 않는다.**
6. **최종 확인:** `make -C harness demo-status`에서 다음 두 가지를 본다.
   - prod가 `트리=v1`이다.
   - v2 PR이 `트리=v2 현재 prod 기준(merge해도 됨)`이다.

## 3. 시연 — v2 PR merge부터 박스 확인까지

| 순서 | 화면·동작 | 말할 것(예시) |
|---|---|---|
| 1 | 관리 페이지 대시보드를 띄운다. 승인 대기 run이 없다. | "지금 운영 중인 v1입니다." 공개 URL의 목록 페이지를 보여 준다. |
| 2 | GitHub에서 열어 둔 v2 PR을 merge한다. | "개발자는 평소처럼 PR을 merge만 합니다." |
| 3 | 관리 페이지에 새 run이 생긴다. 분석·계획이 진행된다. | "prod 변경을 감지해 무엇이 바뀌었는지 분석하고 배포 계획을 세웁니다. 바뀐 것은 WAS뿐입니다." |
| 4 | 승인 화면에서 원본 SHA와 계획·검사 결과를 보여 주고 한 번 승인한다. | "실행 전에는 사람이 한 번 승인합니다. AI는 제안만 합니다." |
| 5 | WAS만 빌드·배포하고 헬스·스모크를 거쳐 SUCCEEDED가 된다. | "web 이미지는 그대로 두고 WAS만 다시 빌드했습니다." |
| 6 | 공개 URL을 새로 고친다. 목록 페이지에 이미지·박스가 보인다. `/version`의 release_id가 새 run_id다. | "v2가 반영됐습니다." |

## 4. 반복

시연이 끝나면 2절로 돌아간다. 순서는 `demo-status` → `demo-reset-v1` → GitHub merge → 관리 페이지 승인 → v1 확인 → `demo-prepare-v2`다. 한 사이클 시간은 아직 재지 않았다. 리허설에서 재서 여기에 적는다.

## 5. 막힐 때

| 증상 | 확인 | 대처 |
|---|---|---|
| merge했는데 새 run이 안 생긴다 | 관리 페이지 설정의 자동 감시 ON, 브랜치 prod, 대상 onprem. 컨트롤러가 merge 전에 떠 있었는지 | 대시보드·`/ops`의 '지금 계획 요청' 버튼이나 `make -C harness onprem-plan PROJECT=<프로젝트>`로 현재 HEAD 계획을 요청한다. |
| 승인 링크가 거부되거나 run이 '대체됨(SUPERSEDED)' | 승인 대기 중에 다른 merge를 했는지 | 새 prod 커밋의 run이 이전 자동 승인 대기를 대체한 것이다. 대시보드의 최신 승인 대기를 승인한다. 대체된 run은 다시 쓰지 않는다. |
| 새 run이 잠금·차단(NEEDS_HUMAN)으로 안 돈다 | 공개 URL·VM 컨테이너가 실제로 어떤 버전인지 운영자가 먼저 확인한다 | 대시보드·`/ops`의 '제품 잠금·차단 해제' 버튼에 사유를 적어 누르거나 `make -C harness unlock PROJECT=<프로젝트>`. 실행 중인 run은 해제되지 않는다. 해제 뒤 '지금 계획 요청'. |
| 계획 요청이 409로 거부된다 | 다른 ref·대상의 승인 대기나 준비 중 요청이 있는지 | 기존 승인 대기를 승인 또는 거절한 뒤 다시 요청한다. |
| 공개 URL이 안 열리거나 옛 화면이 보인다 | 인벤토리 `public_url`. quick tunnel을 다시 띄우면 주소가 바뀐다. `/health/ready`, `/version` release_id | 인벤토리 주소를 고친 뒤 다음 run부터 반영된다. 결과 화면의 주소는 승인 당시 기록이다. 브라우저 캐시는 강력 새로 고침으로 지운다. |
| 스크립트가 "오래된 ... PR이 열려 있다"로 멈춘다 | prod가 그 PR을 만든 뒤에 바뀌었다 | 그 PR을 **merge하지 말고** 닫는다. `gh pr close <번호> --repo 2026-Gerbera/gerbera-application --delete-branch`. 그런 다음 다시 실행한다. |
| 스크립트가 `git clone`·`git push`·`git commit-tree` 실패로 멈춘다 | 출력의 오류(인증 실패, Author identity unknown) | git의 GitHub 인증과 전역 git 신원을 확인한 뒤 다시 실행한다. 원격에는 아무것도 바뀌지 않았다(push 전에 멈춤, 또는 push 실패). |
| `push 완료` 뒤 `gh pr create` 실패로 멈춘다 | gh 로그인·권한 | `demo/` 브랜치만 원격에 남았다. 다시 실행하면 새 브랜치로 PR을 연다. 남은 브랜치는 GitHub에서 지워도 된다. |
| 스크립트가 "변경 없음"을 출력한다 | prod 내용이 이미 목표 태그와 같다 | 정상이다. `demo-status`로 다음 단계를 본다. |
| 스크립트가 "이미 준비된 PR"을 출력한다 | 같은 종류의 PR이 현재 prod 기준으로 열려 있다 | 출력된 URL의 PR을 쓴다. |
| 스모크·헬스 실패 | 결과 화면의 진단, ROLLED_BACK 여부 | 실패한 환경만 롤백된다. 원인을 고친 뒤 '지금 계획 요청'으로 다시 돌린다. |

## 6. 시연 중 주의

- **승인 대기 중에는 merge하지 않는다.** 새 자동 run이 준비되면 이전 승인 대기가 대체됨으로 닫힌다.
- v2 PR은 **v1 되돌림 PR을 merge한 뒤에** 만든다. 그 전에 만든 v2 PR은 오래된 PR이다. merge하면 결과가 v2 트리와 다를 수 있다. merge 직전에 `demo-status`에서 '현재 prod 기준'인지 본다.
- prod에 직접 push나 force push를 하지 않는다. 태그 v1·v2를 옮기지 않는다.
- 준비 중(분석·계획)에는 관리 웹을 재시작하지 않는다. 준비 작업은 컨트롤러 프로세스 수명에 속한다.
- reset-v1·prepare-v2는 태그의 트리를 **그대로** 쓴다. 태그 이후 prod에 넣은 변경(예: 앱 저장소 `deploy.yaml`)도 함께 되돌아간다. 앱 저장소 설정을 바꿨다면 사람이 태그를 다시 맞추고 팀에 알린다.

## 7. 스크립트 안전 조건

- 앱 저장소를 임시 디렉터리에 clone하고 끝나면 지운다. 임시 clone 작업 트리가 깨끗하지 않으면 중단한다.
- 새 커밋은 `git commit-tree`로 만든다. 트리는 태그 트리, 부모는 현재 prod다. 이력을 보존한다. push 전에 트리와 부모를 다시 확인한다.
- push는 `demo/` 새 브랜치 하나뿐이다. `+` 표시와 `--force`가 없고 `--no-follow-tags`로 태그를 보내지 않는다. 같은 이름의 원격 브랜치가 있으면 중단한다. prod 직접 push는 없다.
- gh는 `pr list`(조회)와 `pr create`만 부른다. merge·close는 하지 않는다.
- prod 트리가 이미 목표 태그 트리와 같으면 '변경 없음'으로 끝낸다. 같은 종류의 PR이 현재 prod 기준으로 열려 있으면 재사용한다. 같은 종류의 오래된 PR이 열려 있으면 중단한다.
- 사용자 git 신원과 gh 인증을 그대로 쓴다. git config를 바꾸지 않는다. 오류 출력에서 URL 자격증명과 토큰 모양 문자열을 가린다.
- 시험: `harness/tests/unit/test_demo_cycle.py`. 로컬 bare 저장소로 prod·v1·v2를 흉내 내고 가짜 gh를 PATH에 넣는다. 확인하는 것:
  - 새 커밋 트리가 v1 트리와 같다.
  - 부모가 prod다.
  - force push가 없다.
  - dry-run이 원격을 바꾸지 않는다.
  - 같은 트리면 변경이 없다.
  - 오래된 PR이 있으면 중단한다.
  - 작업 트리가 깨끗하지 않으면 거부한다.
