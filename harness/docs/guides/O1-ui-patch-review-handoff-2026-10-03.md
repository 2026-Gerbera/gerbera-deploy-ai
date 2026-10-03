# 패치 검토 화면 인계 — 2026-10-03

## 기준과 보존

- 작업 위치: `.worktrees/ui-patch-review`, 브랜치 `o1/ui-patch-review`.
- 기준: fetch한 `origin/main` **1d3bb911857a0169dad2d0d85b4d6573c4679bea**. int-1112와 이후 cloud-main-integration을 포함한다.
- `.worktrees/ui-redesign`은 읽기만 했다. 시작 시 기록한 수정·신규 파일 43개의 SHA-256이 종료 검사에서도 일치한다. 이전 인계서와 작업 기록은 새 문서에 합치지 않고 원본에 보존했다.
- 커밋·push·PR·태그·git config 변경 없음. 실제 AWS·VM·Docker·Claude·외부 LLM 호출과 비밀 경로 읽기 없음. 검증용 임시 Git 저장소의 fixture 동작은 작업 브랜치 커밋과 구별한다.

## 사용 흐름

1. 승인 대기 화면의 **제안 선택·수정 요청**에서 검토를 시작한다.
2. 파일별 적용 여부를 선택한다. 한 파일 안의 관련 설정과 공통 import는 하나의 제안이다. 이전 성공 원장에서 재사용하는 수정은 `이전 승인 수정 유지`로 표시하며 선택 해제·재수정할 수 없다.
3. 옆 입력란에서 해당 제안의 수정·추가 검토를 요청한다. 처리 중 표시가 나오고, 기존 제안은 유지한다.
4. 결과를 보고 **새 제안 채택** 또는 **현재안 유지**를 누른다. 미결 후보가 있으면 최종 준비를 막는다.
5. **선택한 수정으로 승인 자료 준비**가 조합 검사와 재계획을 실행한다. 새 run을 만들고 이전 승인은 대체 처리한다.
6. 새 승인 화면에서 **승인하고 배포** 한 번을 누른다. patch/deploy/infra의 승인 기록과 해시는 각각 유지한다.

이 화면의 프롬프트 범위는 main의 P0 설정 패치다. 서명 키·주소·쿠키·프록시 설정을 검토한다. 임의 기능 구현이나 일반 코드 질의응답을 제공하는 채팅 기능은 이번 변경에 포함하지 않았다. 결정적 렌더러가 같은 코드를 만들면 재검토 결과의 코드가 같을 수 있다.

## main에 맞춘 연결

- 최초 제안·재제안·최종 조합 모두 `registry.get("patch_config")`를 경유한다. 기존 `propose_intents → render_intents → prepare_patch`와 엄격 검사·patch ledger·재사용·손실 차단을 사용한다.
- 옛 `generate_review` 직접 생성 경로는 이식하지 않았다. `plan/patch/review.py`에는 조합 검사와 표시용 diff만 둔다.
- 툴의 run ID·status·passed·patch·SHA-256·meta를 검사한다. 최종 선택의 바이트가 검증 결과와 다르면 중단한다. 검사 실패 후 다른 패치로 대체된 결과를 선택 성공으로 처리하지 않는다.
- 재계획 시 새로운 필수 환경 키, 온프렘 공개 파생값, smoke_groups, patch_targets를 반영한다. 양쪽 배포에서 인프라 준비 오류는 main과 같이 cloud 트랙에 격리한다.
- 프로젝트 설정 버전·원본 스냅샷·배포 기준을 재확인한다. 배포 기준은 준비·승인·시작·잠금 획득 뒤에도 검사한다.
- 초안 revision CAS가 오래된 탭·후보·중복 요청을 차단한다. 자식 run 생성과 `publishing` 기록은 같은 DB 트랜잭션이다. 연결 전 자식은 승인 불가이며 실패·재시작 시 취소한다. 부모 대체와 자식 `sealed` 전환도 원자적으로 처리한다.
- 검토 diff는 표시용으로 비밀 리터럴을 구조적으로 가린 뒤 렌더링한다. 승인 바이트와 해시는 변경하지 않는다. 최종 승인 화면의 main 원문 비노출 규칙도 유지한다.
- B안 색·간격 토큰과 `_status.html` 상태 매크로를 사용한다. main의 `connection` 매크로, 설정·온프렘 준비·인프라 승인 화면을 유지했다. 전체 프로젝트 메인과 접을 수 있는 사이드바도 이식했다.

## 추가형 계약과 영향 범위

기존 main 필드는 이름 변경·삭제하지 않았다.

| 위치 | 추가 내용 |
|---|---|
| `core/contracts/patch_review.py` | CodeProposal, ReviewEdit, ProposalBatch, ReviewResult, PatchReviewRequest. 파일 단위 제안과 revision/required/선택/재검토 요청 |
| `core/contracts/tools/patch_config.py` | 입력 `review=None`, 출력 `proposals=[]` |
| `core/contracts/context.py` | 선택적 `review_baseline_hash`. None은 직렬화에서 생략해 기존 context 해시 유지 |
| `core/store.py` | patch_reviews 테이블·CAS·원자 연결·복구. create_run에 선택적 review_parent |
| `executor/service.py` | prepare의 선택적 review_parent, 검토 수명주기와 배포 기준 검사 |

`make -C harness contracts-update`로 스냅샷을 재생성했다. 실제 변경 스키마는 `patch_config.input.json`, `patch_config.output.json` 두 개다. 장민영 패치 툴, 양서윤 관리 웹, 정준우 조립·실행기 담당에게 이 표와 변경 목록을 전달하면 된다.

주요 구현 파일: `app.py`, `core/{store,redact}.py`, `executor/{service,patch_review}.py`, `plan/review.py`, `plan/patch/{generate,pipeline,review,tool_review}.py`, `web/{dependencies,routes,templates,static}`. 테스트는 패치 툴·재계획·조립·HTTP 검토·기존 설정 및 UI 회귀에 분산했다. 전체 경로 목록과 해시는 검증 디렉토리의 `changed-files.json`에 남긴다.

## 검증

검증에는 루트의 기존 `.venv`를 가리키는 임시 링크를 사용했다. 의존성은 설치하지 않았으며 검증 종료 후 임시 링크를 제거했다. 재검증 시 같은 환경을 연결하거나 `UV_PROJECT_ENVIRONMENT=<repo>/.venv`를 지정할 수 있다.

검증 디렉토리는 이 worktree의 `harness/var/validation/ui-patch-review-20261003/`다. Git ignore 대상이므로 인계 시 캡처·로그를 함께 보존해야 한다.

```sh
UV_NO_SYNC=1 PYTHONPATH=<wt>/src make -C harness contracts-update
UV_NO_SYNC=1 PYTHONPATH=<wt>/src make -C harness ci
gitleaks git --redact --no-banner --report-format json \
  --report-path harness/var/validation/ui-patch-review-20261003/gitleaks-git.json .
```

- 최종 CI: **3768 passed, 0 failed, 1 skipped, 4 deselected**, pytest 306.51초, 전체 309.4초. lint/type/boundary/contracts/test 모두 PASS, exit 0. `ci-final.log`. 1 skipped는 별도 temp-box 프로젝트가 필요한 기존 Flask 앱 테스트다.
- Git 이력 검사: **212 commits, 발견 0건, exit 0**. `gitleaks-git.log/json`.
- 미커밋 변경 파일 별도 사본의 `gitleaks dir` 검사도 발견 0건이다. `gitleaks-changed.log/json`. 증거 테스트 사본은 `test_*.py.evidence.txt`로 보관해 pytest 중복 수집을 막았다.
- 원본 보존: `source-preservation.json`, `source-preservation-final.json` — 43개 확인, 변경 0개.
- 독립 PRE는 기존 경로를 그대로 이식하면 승인 메타·원장·필수 키·스모크·cloud 격리·연결 복구·diff 노출 문제가 있다고 판정했다. 해당 경계를 반영해 구조를 수정했다.
- POST에서 발견한 타입 주석 비밀 리터럴 노출을 보완하고 실제 render→제안→현재/후보 표시 회귀로 확인했다. 외부 Claude 검토는 사용자 금지에 따라 실행하지 않았다.
- 최종 독립 집중 검토는 **PASS**, 관련 fake unit **155 passed, 53.19초**다. 타입 주석·인접 리터럴·삼중 따옴표·UTF-8 위치의 표시 가림, 승인 패치 바이트 불변과 CRLF 재조합을 확인했다. 일부 여러 줄 리터럴 수정·혼합 줄바꿈은 기존 검사에서 거부됐으므로 지원 성공으로 세지 않았다. `post-review-final.json`에 범위와 결과를 보존했다.
- 첫 전체 검사는 13 failed / 104 errors였다. 원인은 옛 재계획 기대치·옛 조립 fixture·설정 화면 테스트 대역의 list_projects 누락이었다. 최종 성공 결과와 혼동하지 않도록 `ci-first.log`도 보존한다.

## 브라우저 확인과 캡처

로컬 fixture 서버에서 실제 등록 패치 툴·검사기·저장소·HTTP 라우트를 사용했다. 생성 응답, 재계획 결과, 인프라와 배포 어댑터는 fixture다. 화면에 보이는 배포 성공은 실제 인프라 검증을 뜻하지 않는다.

- 제안 목록, 후보 비교, 새 승인, 결과를 **1440×1000, 1280×1000, 390×1000**에서 열었다. 12개 조합 모두 `scrollWidth == clientWidth`. 코드·긴 해시는 내부 줄바꿈을 사용한다. `browser-layout.json`.
- 일부 선택→옆 프롬프트→재검토 진행→후보 채택→재검증 진행→새 승인→한 번 승인→결과를 브라우저로 조작했다.
- 승인 기록은 deploy/infra/patch 3종, 서로 다른 대상 해시, 동일 approval_id 1개다. 부모는 SUPERSEDED, 자식은 fixture SUCCEEDED. `browser-approval-records.json`.
- 표시 가림 보완 후 최종 제안 화면을 세 폭에서 다시 확인했고 모두 넘침 0이다. `browser-layout-final.json`.
- 전체 프로젝트 홈과 사이드바 접기도 확인했다. 데스크톱·노트북 위주로 시각 검토했고 모바일은 넘침·배치만 간단히 확인했다.

| 화면 | 캡처 경로(검증 디렉토리 기준) |
|---|---|
| 파일별 제안·옆 프롬프트 | `review-final-1440.jpg`, `review-final-1280.jpg`, `review-final-390.jpg` |
| 기존안·새 후보 비교 | `candidate-1440.jpg`, `candidate-1280.jpg`, `candidate-390.jpg` |
| 새 승인 화면 | `approval-1440.jpg`, `approval-1280.jpg`, `approval-390.jpg` |
| 결과 | `result-1440.jpg`, `result-1280.jpg`, `result-390.jpg` |
| 생성·재검토·재계획 진행 | `generating-1440.jpg`, `revising-1440.jpg`, `finalizing-1440.jpg` |
| 한 번 승인 후 진행 화면 | `post-approval-1280.jpg` |
| 전체 메인·사이드바 접기 | `overview-1440.jpg`, `sidebar-closed-1440.jpg` |

## 남은 운영 확인

실제 provider 응답 품질, AWS·VM·Docker를 쓰는 배포 및 3분 시연은 이 작업의 검증 범위 밖이다. 이전 성공 원장의 필수 수정은 이 UI에서 제거할 수 없다. 원장 손실이나 파일 단위 검사 실패가 생기면 승인 전에 중단하며, 범위를 넓히려면 패치 계약을 별도로 설계해야 한다.
