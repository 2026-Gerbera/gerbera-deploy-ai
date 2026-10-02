# 수정10 — 관리자 UI 통합 인계 (2026-10-03)

## 범위와 Git 상태

- 작업: `.worktrees/ui-int`, `o1/ui-integration`, 시작 `origin/o1/exec-fix8` **83e0b69**.
- `origin/cloud` **486b4b1**을 `git merge --no-commit --no-ff`로 통합했다. app.py import 충돌은 양쪽 합집합으로 해결했다. 커밋하지 않았으므로 **MERGE_HEAD가 있는 미커밋 merge 상태**다. 충돌 표식/미해결 index는 없다. staged merge 변경과 unstaged 추가 수정, 신규 파일을 함께 검토·stage한 뒤 정준우가 merge 커밋을 마감한다.
- 원래 작업 트리 및 fix8-snap은 변경하지 않았다. 실제 AWS/VM/Docker/Claude, 프로세스 종료 명령, commit/push/PR은 실행하지 않았다.
- 배경: 정준우가 실환경 성공으로 전달한 `run-20261002-175023-84a6`은 이번 작업에서 재실행하지 않았다. 아래 검증은 fixture 기반이다.

## 서윤 확인 필요 — 이번에 바꾼 연결점

| 영역 | 변경 및 사용 경로 |
|---|---|
| app 조립 | `preflight_local_build`, `fixture_binding`, `missing_track_tools`, `seed_registry_secrets`, `run_dir`, `tool_context` 유지. `refresh=_refresh_cloud_context`와 generate_infra의 tool_context 유지. sync_env_to_cloud는 cloud 브랜치 등록 구현을 그대로 사용. |
| 프로젝트 | `web/dependencies.py`의 selected_project: 명시 프로젝트 → DDAK_WATCH_PROJECT → flaskr, 기존 demo 별칭은 service.resolve_project로 유지. dashboard/settings/ops가 동일한 선택을 사용. 기본 대상은 ProjectSettings(onprem). |
| 설정 | 도메인 없는 onprem 저장 허용. cloud/both는 기존 도메인 검증 유지. `/settings/deploy`는 `/ops/plan`과 동일한 service.enqueue_deployment 사용; 준비를 백그라운드에서 진행하고 즉시 프로젝트 대시보드로 이동. 중복은 서비스가 판정. |
| 대시보드 | 선택 프로젝트 기록만 표시. AWAITING_APPROVAL은 승인, 진행 상태는 progress, 종료 상태는 result. `_ops.html`에 현재 HEAD 계획·사유 입력 잠금 해제·준비 상태. HTML unlock은 대시보드로 복귀하고 CLI용 JSON 응답은 유지. |
| 승인 | `/runs/{id}/approval`이 단일 renderer. 이전 `/ops/runs/{id}/approval`도 같은 핸들러/템플릿을 사용. 중복 ops_approval.html 삭제. source_sha·repo/ref·step ID/tool/target·준비 경고/실패·source 검사 예외·patch 메타·infra 요약·기존 subjects 해시 표시. 승인 저장/해시 재검사 로직은 그대로. |
| 승인 실패 | AWAITING_APPROVAL이 아닌 run은 결과로 303. 승인 데이터 조회의 DdakToolError도 결과로 보냄. POST 충돌은 가린 409, CSRF 유지. inline diff는 그대로 표시하되 승인 전에는 404인 '승인된 Diff 다운로드' 링크를 제거. |
| 결과 | SUCCEEDED에만 봉인 완료 표시. 실패 진단 phase/code/detail/missing_tool 및 step error를 redact/escape해 표시. 값이 없는 항목은 `—`로 표시. infra_changes가 있는 실패는 인프라 변경 경고를 먼저 표시하며 '인프라 변경 없음'으로 오인하지 않게 함. |
| 진행 | 종료 상태는 서버의 RunStatus + SUPERSEDED에서 전달. 종료 run 재접속과 이벤트 없는 SUPERSEDED도 끝남. step 없는 이벤트는 phase를 바꾸지 않음. |
| 공개 주소 | service.get_run의 context.public_url에 승인 당시 onprem 인벤토리 주소만 노출. 대시보드·결과에서 공개 주소와 `/version` 링크 표시, cloud는 실행 스냅샷의 cloud_domain 사용. HTTP(S)만, 자격증명/query/fragment 링크는 제외. 최근 기록의 주소이며 현재 도달 가능하다는 판정은 아님. |

## 파일 목록

- 통합 추가 수정: `src/ddak/app.py`, `core/project_settings.py`, `core/store.py`, `core/contracts/infra_outputs.py`, `executor/service.py`, `web/dependencies.py`, `web/routes/{pages,settings,approvals,results,events,ops}.py`, `web/static/app.js`, `web/templates/{dashboard,settings,approval,result,progress,base,_ops,ops}.html`, `_public_links.html` 신규, `ops_approval.html` 삭제.
- 테스트: `harness/tests/unit/test_web.py`, `test_infra_preparation.py`(생성기 미등록 fixture 격리), `test_ui_integration_fix10.py` 신규, `test_ui_settings_fix10.py`·`test_watch_duplicates_fix10.py` 신규.
- 개발 의존성: `pyproject.toml`, `uv.lock`에 TestClient가 요구하는 **httpx2** 추가. 제품 runtime 의존성은 그대로. 기존 shared venv 대신 이 worktree 전용 .venv를 만들었다.
- cloud 브랜치에서 수입: generate_infra 4파일 및 테스트, cloud/infra/assembly.py, cloud/deploy의 registry_secrets 및 provider/public export, cd/tools/sync_env_to_cloud 2파일, cloud/health 2파일 및 테스트, C3 CSS/템플릿/라우트. 이 기능의 정책을 이번 UI 작업에서 확장하지 않았다.
- 문서: 이 인계서, `harness/docs/decisions/2026-10-03-ui-integration.md`, `harness/docs/ai-usage/O1.md`.

## NEEDS_CONTEXT 및 남은 요청

1. **서윤·준석·정준우: 출력 층 경계.** ecs_service_name/app_security_group_id/target_group_arn 중복을 제거했다. 지시대로 PLATFORM_OUTPUTS에 task_execution_role_arn/task_role_arn/dbinit_execution_role_arn/app_secret_arn_SECRET_KEY를 유지했다. APP 층과 중복 허용 상태이며, 플랫폼 최초 생성과 후속 앱 변경의 소유권은 합의 필요. 허용 목록을 되돌리거나 런타임 검사를 우회하지 않았다.
2. **준석·C1: generate_infra 프롬프트 ↔ 게이트 정합.** prompt.md의 ECS service 안내에는 policy.py가 필수로 요구하는 lifecycle ignore_changes(task_definition, desired_count)가 명시돼 있지 않다. AI가 우연히 생성할 것에 의존하지 않도록 정합을 맞춰야 한다. 전체 AWS 배포는 이번에 검증하지 않았다.
3. **준석·정준우: 호출 제한 시간.** generate_infra 등록 제한은 300초이고 app 조립은 wait_for/deadline을 적용한다. 동기 AI 호출의 실제 provider timeout과 남은 deadline 전달, 시간 초과 후 스레드/번들 격리 동작을 실호출 전에 확인해야 한다. 이번 작업은 호출 금지라 확인하지 않았다.
4. **서윤·준석: DNS 결정.** 설정은 external DNS도 허용하지만 실제 generate_infra._safe_data는 Route 53 hosted_zone_id를 요구한다. 첫 클라우드 배포의 지원 DNS와 UI 설명을 합의해야 한다. both의 cloud 준비 오류가 local까지 막지 않는 회귀는 유지했다.
5. **서윤·정준우: Host 허용 목록.** 현재 POST는 정확히 127.0.0.1:<port> Host/Origin과 CSRF를 요구한다. localhost/WSL 호스트 IP를 쓰려면 별도 결정이 필요하다. CLI 연결 관리 페이지의 외부 공개 금지는 유지하며 이번에는 허용 범위를 넓히지 않았다.
6. **승환·서윤: 첫 cloud registry 준비.** seed_registry_secrets는 cloud 플랫폼 apply 뒤 호스트 환경의 Docker Hub 자격증명을 사용한다. 로컬 빌드가 기존 docker login을 쓰는 경로와 구분된다. 이번에는 토큰을 읽거나 실제 seed를 호출하지 않았다.
7. 실환경 후속: 정준우 데스크탑에서 UI pull 후 동일 프로젝트/인벤토리로 화면을 확인한다. public_url은 실행 스냅샷이므로 quick tunnel 주소가 바뀌면 인벤토리와 다음 실행 기록도 갱신해야 한다.

## 검증

- FastAPI TestClient의 성공/실패: dashboard → approval → POST 승인 → progress → result, 같은 승인 해시와 양 승인 URL 동일 화면 확인.
- 온프렘 public_url/version 및 cloud_domain 링크, flaskr-three 설정/운영/대시보드 일치, SUPERSEDED 승인 거부/SSE 종료, 실패 진단 redact 및 인프라 변경 경고, CSRF/unlock, 기존 수정8 준비 격리 회귀 포함.
- Node(기존 pyright[nodejs] 런타임)로 실제 app.js를 실행해 step 없는 이벤트가 phase를 유지하고 FAILED_VERIFY로 종료하는지 확인. 새 JS 의존성 없음.
- 최종 수치와 시각은 아래 마감 기록 및 `harness/var/validation/fix10-20261003/`에 남긴다. CI 소요 시간은 배포 벤치마크가 아니다.

## 제안 커밋 메시지

`feat: 관리자 콘솔 통합과 중복 자동 감시 차단`

merge 중이므로 위 제목의 한 merge 커밋으로 수입 변경과 통합 수정을 함께 마감하는 안이다. 분리된 문서 커밋을 원하면 merge 마감 후 `docs: 관리자 UI 통합 검증 및 담당자 인계 기록`을 사용한다.

### 수정10 기본 UI 검증 — 추가1 전 기록

- **gitleaks 있음 전체 CI**: `1829 passed, 1 skipped, 4 deselected in 160.83s (0:02:40)`. 전체 **165.697초**, exit 0, 완료 **2026-10-03T03:26:09.806988+09:00**.
- **gitleaks 없는 PATH 전체 테스트**: `1810 passed, 20 skipped, 4 deselected in 134.12s (0:02:14)`. 전체 **134.757초**, exit 0, 완료 **2026-10-03T03:25:38.928105+09:00**.
- lint/type/import boundary/contracts 모두 PASS. 실제 scanner 테스트 19개만 미설치 환경에서 추가 skip했다. 공통 skip 1개는 별도 temp-box 앱 테스트, deselected 4개는 외부 실행 마커다. 제품 scanner fail-closed는 유지했다.
- 최종 UI/ops 선택 회귀 71개와 인프라 준비 fixture 7개를 포함한다. 독립 검토 후 바뀐 두 UI 경로와 기존 미등록 테스트를 고친 다음 전체 검증을 다시 완료했다. 두 전체 검증은 병렬 실행했고 CI 90초 목표는 초과했다. 배포 시간 수치로 사용하지 않는다.
- 정확한 명령·시각·종료 코드·초기 실패·최종 로그는 `harness/var/validation/fix10-20261003/`에 보존했다. `git diff --check` 및 미해결 merge index 없음 확인. 실제 WSL/VM 재실행은 남아 있다.


## 추가1 — 같은 저장소·브랜치의 중복 자동 감시 방지

- **저장:** 같은 repo_url·watch_branch를 자동 감시하는 다른 프로젝트가 있으면 CONFIG_INVALID로 거부하고 소유 프로젝트명을 표시한다. 부분 설정 갱신에도 적용하며, 검사와 저장이 같은 `BEGIN IMMEDIATE` 안에 있어 동시 저장도 하나만 성공한다. 자동 감지 OFF, 다른 저장소/브랜치, 정상 소유자의 기존 설정 갱신은 허용한다.
- **이미 중복인 상태:** app 조립에서 repo/branch별 WatchTarget을 하나만 넘긴다. 후보 중 `DDAK_WATCH_PROJECT`가 있으면 우선, 없으면 flaskr 우선/프로젝트명 순서로 안정적으로 선택한다. 제외된 프로젝트와 선택된 프로젝트명을 대시보드·`/ops`·설정 화면과 컨트롤러 경고 로그에 표시한다. 기존 설정이나 실행 기록을 자동 삭제하지 않는다. O2 `watch.py`는 바꾸지 않았다.
- **표기 차이:** GitHub 호스트·경로 대소문자, `.git`/끝 슬래시, 기본 HTTPS 포트, 브랜치 `refs/heads/` 접두어를 정규화한다. 다른 호스트의 경로 대소문자는 보존한다. URL의 모든 별칭이나 서로 다른 mirror 저장소를 자동 추론하지 않는다. 잘못된 포트의 신규 설정은 저장 때 거부하고, 과거 잘못된 URL은 감시에서 제외해 경고한다.
- **설정 우선순위:** 이미 저장된 프로젝트 설정이 환경변수 fallback보다 우선한다. 저장된 auto_detect=false를 환경변수로 다시 켜지 않는다. 다른 프로젝트가 같은 소스를 자동 감시할 때 onprem 프로필의 초기 설정 복사는 생략하고, 환경변수 후보 선택·경고를 사용한다.
- **정준우가 할 정리:** `DDAK_WATCH_PROJECT=flaskr-three`로 기동한 뒤 `/settings?project=flaskr`에서 기존 flaskr의 자동 변경 감지를 끄고 저장한다. 그 뒤 flaskr-three 설정을 저장한다. 이미 생긴 flaskr의 중복 AWAITING_APPROVAL은 해당 run의 승인 화면에서 **거절**한다. 이 변경은 기존 승인 대기 run을 자동 취소하지 않는다.
- **회귀:** 동시 저장, 부분 설정/버전 보존, URL/ref 표기 차이, 기존 중복 및 환경변수 fallback, 비활성 설정 보존, 잘못된 과거 URL, 실제 FastAPI lifespan + 가짜 Watcher의 커밋 1회 처리/실행 1개 및 3개 화면 경고를 확인했다. 새 테스트는 source=fixture이며 실제 Git/VM 연결이 없다.
- 추가1 선택 회귀: 최초 99 passed. 검토 후 설정 화면·잘못된 URL 회귀를 보강해 53 passed(2.54초). 테스트가 ValidationError 대신 DdakToolError를 기대했던 1건은 실제 서비스 계약에 맞춰 수정했다. 마지막 전체 CI 수치는 아래 추가1 포함 마감 기록을 따른다.


### 수정10 추가1 포함 최종 마감 — DONE

- **gitleaks 설치 환경 전체 CI:** `1843 passed, 1 skipped, 4 deselected in 158.85s (0:02:38)`. 전체 **161.951초**, exit 0, 완료 **2026-10-03T03:42:22.383581+09:00**. lint/type/import boundary/contracts PASS.
- **gitleaks 없는 PATH 전체 테스트:** `1824 passed, 20 skipped, 4 deselected in 132.37s (0:02:12)`. 전체 **133.113초**, exit 0, 완료 **2026-10-03T03:41:54.690204+09:00**. 실제 scanner 회귀 19개가 추가 skip되며 제품 fail-closed는 유지했다. 공통 skip은 별도 temp-box 앱 1개, 외부 실행 마커 4개는 deselected다.
- 독립 최종 검토 Ohm은 추가1 코드/회귀 범위에서 도달 가능한 결함 없음을 확인했다(읽기 전용, 테스트 미실행). 실제 실행 증거는 주 세션의 위 CI다. 외부 Claude 및 실제 AWS/VM/Docker 호출은 없었다.
- UI 성공/실패 TestClient 동선과 수정8 `/ops`, 트랙 준비 격리, 중복 자동 감시 동시 저장 및 기동 1회 동작이 모두 포함됐다. 전체 검증은 병렬 실행했고 시간은 배포 벤치마크가 아니다. CI 90초 목표는 초과했다.
- 변경 파일 **50개**(cloud merge 수입 및 신규 파일 포함). 전체 목록/명령/시간/종료 코드는 `harness/var/validation/fix10-20261003/{changed-files,summary}.json` 및 두 로그에 있다. 기본 UI의 이전 검증은 `*-before-add1`로 보존했다.
- `git diff --check`, 미해결 index 없음 확인. 미커밋 merge 상태를 유지했다. **정준우가 stage·merge 커밋·push 후 WSL에서 실제 화면을 확인**한다. 기존 flaskr 중복 감지는 OFF로 저장하고, 이미 생긴 중복 승인 대기는 거절해야 한다.
