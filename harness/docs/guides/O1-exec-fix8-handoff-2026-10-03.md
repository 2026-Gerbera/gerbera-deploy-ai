# 수정 8 인계 — 10/3

## 결과와 범위

O1 코드는 구현했고 승인 전 검사·후보·local build·운영 API를 가짜 runner/provider로 연결했다. 실제 AWS·VM·Docker build·Claude 호출은 하지 않았다. 원래 브랜치는 o1/exec-fix6, 분리 연결분은 `.worktrees/o1-o2-link`의 o1/o2-link다. 이 개발 세션은 커밋·push·PR을 하지 않았다. 연결분은 별도 담당자가 커밋했고 마지막 확인 HEAD는 84f69cb다. 해당 worktree의 `.venv`는 개발용 링크이므로 커밋 대상에서 제외한다.

| 항목 | 구현 결과 |
|---|---|
| AI 선택/이름 | Groq 전용 키·클래스·실제 provider/model, Claude 판단 adapter, Sonnet5.5/low·medium 선택, TypeSafe 키 미전송 |
| local build | 승인 ai-prod SHA checkout, 패키지 buildspec 재사용, 기존 Docker 로그인, buildx amd64/arm64 사전 검사, digest ReleaseArtifacts |
| 계획/온프렘 | migration 키 공통 금지, BOOTSTRAP 키 신규, OPTIONAL 미등록 제외, 필수 미등록 환경만 실패 |
| 소스 검사 | prod preflight 승인 전, 원본 정확한 fingerprint 예외, 변경 파일 엄격 검사·안전 요약 |
| 승인/잠금 | 자동 승인 대기 supersede, controller lease, 선택 환경 차단, 수동 해제/감사·원문 실패 보존 |
| 조립/운영 | 설정 감시 갱신, 현재 HEAD/태그 수동 요청, /ops 페이지·안전 POST, make 진입점 |
| 런북 | [onprem-fullchain-runbook.md](onprem-fullchain-runbook.md) |

## 실행·검증

사용자가 검증보다 개발 완료를 우선하도록 변경하여 전체 CI 반복은 생략했다. 연결분의 전체 검사에서 발견한 3건은 고쳐 최소 회귀로 검증했다. 최종 전체 CI green을 주장하지 않는다. 명령·결과·소요 시간은 `harness/var/validation/followup8-20261003/` 및 연결 worktree의 `harness/var/validation/o1-o2-link-20261003/`에 남긴다.

- 연결분: 정적 lint/type/boundary/contracts 통과, 관련 376 passed, 실패 경로 56 passed, 최종 gitleaks 없는 핵심+Git 왕복 58 passed.
- 원래 수정8: 정적 lint/type/boundary/contracts 통과, 경계 48 passed. 최초 통합 선택 285 passed/2 failed(102.648초), 두 건 수정 뒤 후보·준비·운영 31 passed(8.02초).
- Store가 제외했던 후보 서비스 5건도 수정 후 후보 서비스 파일 전체에서 통과했다. TypeError 시그니처 불일치는 해소됐고, 취소 테스트의 대기는 승인 전 검사가 아니라 실제 후보 생성 검사에 걸도록 수정했다.
- local backend 포함 실제 로컬 bare Git 왕복 v1→이미지/박스 v2→v1의 4개 변형 통과. 빌드·VM은 가짜다. 기록의 fixture 시간은 실환경 벤치마크가 아니다.
- 마지막 gitleaks 없는 선택 회귀는 38 passed/1 failed(22.94초). 실패는 취소 테스트가 승인 전 검사에도 실제 scanner를 요구한 fixture 오류였다. 통과 scanner를 명시 주입한 뒤 해당 파일 전체 6 passed(6.06초). 제품 scanner 부재 시 차단은 유지했다.
- 추가5 최종 회귀: `make -C harness test ARGS='tests/unit/test_fix8_add5.py tests/unit/test_app_preparation.py tests/unit/test_fullchain_fix8.py -q'` → 29 passed(0.64초). cloud 인프라 준비 실패 격리, 즉시 202, ref/대상별 중복 처리, 자동 준비 직렬화 포함.
- 읽기 전용 독립 검토: 연결분의 트랙 격리/모델 혼선을 고쳤다. 추가5에서 다른 ref/대상의 조용한 재사용, 준비 경합으로 자동 감시 커밋을 잃을 가능성을 발견해 명시 거절·비동기 대기로 수정하고 위 29개 회귀로 확인했다. 외부 Claude 리뷰는 실제 호출 금지에 따라 수행하지 않았다.

## 바로 실행하기 전에 남은 위험

1. 로컬 Docker 로그인 판정은 system info의 Username 메타데이터에 의존한다. 실제 WSL Docker/credential helper와 호환 여부, push 권한은 미검증이다. buildx/QEMU는 준비돼 있어야 한다.
2. 현재 temp-box는 `/version`에 db.dialect가 없고 ready가 ok bool을 반환한다. 현재 smoke는 status=ok와 MySQL dialect 및 게시판 동작을 요구한다. 실제 gerbera-application 체크아웃은 확인하지 못했다. 앱/스모크 계약을 민영님이 맞추지 않으면 실제 배포가 성공해도 스모크에서 실패한다.
3. 자동 3tier 첫 계획의 DB 우선 순서를 O2가 거부한다. 리허설은 deploy.yaml에서 DB tier를 빼고 기존 DB·마이그레이션 연결을 사용한다. DB/볼륨을 삭제하지 않는다.
4. onprem만 배포할 때 cloud의 이전 manifest=None이 변경 탐지에 들어가 전부 변경으로 보일 수 있다. 재빌드/재배포가 늘며 시간에 영향을 준다.
5. required(name) 분석 미지원은 runtime env와 public_env로 필요한 설정을 준비한다. 분석 분류 자체는 요청 범위 외라 유지했다.
6. 첫 watcher HEAD는 기준선만 기록하며 준비 실패는 기존 3회 재시도 정책이다. 현재 HEAD 재계획 경로를 제공했다. 시연은 컨트롤러 시작 후 PR merge한다.

## 담당자 전달 — 확인 필요

### 준석

- 연결분의 AI/분석/validate/watch 변경 목록은 별도 [연결분 결정](../decisions/2026-10-03-o1-o2-link.md)을 참조한다. 합칠 때 main의 연결분을 기준으로 한다.
- DDAK_GROQ_API_KEY·DDAK_GROQ_MODEL로 셸 설정 이동. DDAK_JEV_BACKEND 판단 선택과 DDAK_WATCH_PROJECT/TARGETS/BRANCH 기본 flaskr/local/prod 확인.
- `plan/flow.py:122` 이전 manifest를 요청 대상 환경으로 한정한다.
- `plan/validate/rules.py:59`의 모든 deploy_tier 선행 거부에서 DB 초기 배포를 구분해 db→migrate→was→web을 허용해야 한다. cloud에서는 RDS이므로 DB tier build/deploy 제외.
- `plan/analyze/rules.py:12` required(name) 및 내부 기본값 분류. AI가 plain으로 내리는 현재 동작과 README 승격만 설명을 맞출지 결정 필요.
- watcher 첫 기동/결정적 준비 실패 재시도 정책 개선은 아직 요청 사항이다.

### 승환

- `cloud/build/local.py`, build `__init__.py`/`image.py`의 설정 분기·공유 buildspec 소비 변경 확인 필요. CodeBuild 본래 경로 유지.
- `cloud/deploy/entry.py:83`에도 core.env_keys의 runtime migration 키 검사를 적용해야 Fake/온프렘과 같아진다. 이번에 수정하지 않았다.
- 공개 저장소의 토큰 값은 코드·문서·로그에 없다. local은 기존 사용자 Docker 로그인을 쓴다.

### 서윤 — 합칠 연결 지점

- 기존 approvals.py/settings.py/dashboard·result·approval 템플릿/정적 파일은 수정하지 않았다.
- 새 `web/routes/ops.py`와 `ops.html`, `ops_approval.html`, `_ops.html`. app.create의 include_router 연결만 병합한다.
- dashboard/result의 `content` 블록에서 hero 다음에 `{% include '_ops.html' %}`를 넣을 수 있다. project 및 선택 run 변수를 받는다. 대체됨은 SUPERSEDED다.
- 승인 데이터: preparation_failures, source_checks(source/ignored_count/findings), patch_meta, infra_summary. `/ops/runs/{run_id}/approval`은 이 정보를 보여주고 기존 POST 승인/실행 경로를 쓴다. 기존 GET 승인 라우트의 DdakToolError는 4xx로 처리해야 한다.
- 설정 저장은 공개 save_project_settings(expected_version), 배포 버튼은 enqueue_deployment(project, targets=None, ref=None)를 쓴다. request_deployment는 완료까지 기다리는 내부 API로 남긴다.
- project_state.blocked_targets로 차단을 표시한다. NEEDS_HUMAN 원문은 해제 후에도 남는다. unlock_project(project, actor, reason), unlock_history(project, limit=20) 공개 API.
- `verify/report/rules.py:12`의 cloud_domain_missing high 경고를 onprem 단독에서 제외해야 한다. 기존 SSE /events 501도 담당 확인.
- origin/cloud의 sync_env_to_cloud 등록은 그대로 수용할 예정이며 이 작업에서 중복 구현하지 않았다.

### 민영

- 위 앱 응답과 smoke 계약 확인이 실환경 차단 요소다. 이번에 앱/verify 파일은 수정하지 않았다. 로그인 개발은 하지 않았다.
- patch 검사 passed/patch_sha256를 prepare에 요구하는 계약은 아직 NEEDS_CONTEXT다. alias import·정상 SECRET_KEY 변경 오탐 등 앞선 요청을 유지한다.

## 결정 필요 / AGENTS 제안

- 생성 역할 api.py는 여전히 Groq이며 출처 claude-api 이름이 남는다. 이번 이름 변경 범위 밖이라 결정 필요.
- AGENTS 이미지 행 추가안: “기본 빌드는 CodeBuild, 온프렘 리허설·개발은 설정으로 local을 선택하며 동일 플랫폼 buildspec과 digest 계약을 사용한다.” AGENTS는 수정하지 않았다.

## 제안 커밋·PR

연결분을 먼저 merge한 뒤 나머지 코드·테스트 / 문서를 나눠 커밋한다. 현재 원래 트리에는 연결분과 동일한 수정이 있으므로 main 반영분을 우선해 중복을 정리한다.

1. `feat: 온프렘 로컬 빌드와 승인 전 검사 및 운영 복구 연결`
2. `docs: 온프렘 풀체인 실행 절차와 수정8 인계 정리`

PR 제목: `온프렘 로컬 풀체인 실행과 운영 복구 연결`

PR 본문: CodeBuild와 같은 buildspec을 사용하는 local backend, 승인 전 소스 검사, 자동 승인 대기 대체, 잠금 해제·감사 및 /ops 설정·재계획 경로를 연결했다. 기존 C3 화면은 건드리지 않고 새 라우트/템플릿으로 분리했다. 가짜 빌드/provider와 로컬 bare Git 왕복으로 검증했으며 실제 Docker/VM/Claude 실행은 수행하지 않았다. 남은 앱 스모크 계약·DB 계획 이슈는 담당자 요청에 기록했다. 개발 기록은 `harness/docs/ai-usage/O1.md`다.

## 추가 5 반영

- `generate_infra` CONFIG_INVALID 포함 _infra_approval의 DdakToolError를 cloud 트랙 준비 실패로 격리. local은 승인 대기·실행 유지, 실패 cloud는 apply/rollback 없이 실패 기록. 미등록 sync_env_to_cloud 검사와 독립적이다.
- app의 request.target 비교는 onprem 대신 local 사용. cloud 출력이 없는 local 요청에 cloud platform을 만들지 않는다.
- **서윤 /settings/deploy 연결:** 동기 대기 대신 `service.enqueue_deployment(project: str, *, targets: Literal['onprem','cloud','both'] | None = None, ref: str | None = None) -> dict`를 호출한다. 응답은 request_id/status/run_id(준비 중 None)이다. 동일 프로젝트·ref·대상의 준비/승인 대기만 재사용한다. 다른 ref/대상이 대기 중이거나 자동 계획이 준비 중이면 LOCK_HELD(HTTP 409)로 명확히 거부한다. `get_preparation(request_id) -> dict`, `list_preparations(project) -> list[dict]`로 조회한다. `/ops/plan`은 202, 브라우저는 목록으로 즉시 이동한다.
- `/ops` 준비 중 표시는 2초 새로고침. 준비 오류의 code/detail 또는 생성된 run으로 이어진다. 컨트롤러 종료 시 준비 task를 취소·회수한다. 준비 요청의 메모리 표는 재시작을 넘기지 않는다.
- 자동 감시끼리 준비가 겹치면 프로젝트별 asyncio.Lock에서 기다린다. 준비 경합을 watcher의 실패 재시도로 소비하지 않는다. 완료된 새 자동 run의 supersede 규칙은 그대로다.
- 추가5 최초 19 passed(1.10초), 독립 검토 수정 후 최종 29 passed(0.64초). 자세한 검사 기록은 위 실행·검증 절을 따른다.

### 서윤 cloud 브랜치 병합 체크

1. app.py import는 합집합으로 병합한다: preflight_local_build, missing_track_tools, fixture_binding, seed_registry_secrets, run_dir, tool_context. 이 트리에는 아직 없는 서윤 브랜치의 seed_registry_secrets 구현을 임의로 만들지 않았다.
2. 서윤의 `refresh=_refresh_cloud_context`를 유지한다. 현재 트리의 refresh_infra_context 연결을 단순 덮어쓰지 말고 클라우드 refresh 함수 내부에 이어야 한다. `with tool_context("generate_infra", ...)`도 남긴다.
3. dashboard content 블록의 hero 다음에 `_ops.html`을 include한다. 기존 dashboard 파일은 이번 작업에서 고치지 않았다.
4. 기존 C3 승인 페이지에 source_checks/preparation_errors/patch_meta/infra_summary 표시를 합친다. 그전에는 `/ops/runs/{run_id}/approval`을 쓴다.

## 바뀐 파일 목록

- `harness/Makefile`
- `harness/contracts/schemas/analyze_project.output.json`
- `harness/contracts/schemas/generate_plan.input.json`
- `harness/contracts/schemas/validate_plan.input.json`
- `harness/docs/ai-usage/O1.md`
- `harness/docs/decisions/2026-10-03-o1-exec-fix8.md`
- `harness/docs/decisions/2026-10-03-o1-o2-link.md`
- `harness/docs/guides/O1-exec-fix8-handoff-2026-10-03.md`
- `harness/docs/guides/O1.md`
- `harness/docs/guides/O2.md`
- `harness/docs/guides/onprem-fullchain-runbook.md`
- `harness/scripts/dev.py`
- `harness/scripts/onprem_fullchain.py`
- `harness/tests/e2e/test_g_roundtrip.py`
- `harness/tests/unit/cloud/build/test_local.py`
- `harness/tests/unit/core/ai/test_claude_judgment.py`
- `harness/tests/unit/core/ai/test_groq_providers.py`
- `harness/tests/unit/core/test_candidate.py`
- `harness/tests/unit/plan/analyze/test_analyze.py`
- `harness/tests/unit/plan/intake/test_watch.py`
- `harness/tests/unit/plan/planner/test_planner.py`
- `harness/tests/unit/plan/test_flow.py`
- `harness/tests/unit/plan/validate/test_validate.py`
- `harness/tests/unit/test_candidate_service.py`
- `harness/tests/unit/test_deployment_service.py`
- `harness/tests/unit/test_exec_fix8.py`
- `harness/tests/unit/test_fix8_add5.py`
- `harness/tests/unit/test_fullchain_fix8.py`
- `harness/tests/unit/test_infra_preparation.py`
- `harness/tests/unit/test_store.py`
- `harness/tests/unit/test_store_fix8.py`
- `src/ddak/app.py`
- `src/ddak/cd/fake.py`
- `src/ddak/cloud/build/__init__.py`
- `src/ddak/cloud/build/image.py`
- `src/ddak/cloud/build/local.py`
- `src/ddak/core/ai/gateway.py`
- `src/ddak/core/ai/providers/__init__.py`
- `src/ddak/core/ai/providers/api.py`
- `src/ddak/core/ai/providers/claude.py`
- `src/ddak/core/ai/providers/cli.py`
- `src/ddak/core/ai/providers/jev.py`
- `src/ddak/core/ai/status.py`
- `src/ddak/core/app_repository.py`
- `src/ddak/core/candidate.py`
- `src/ddak/core/config.py`
- `src/ddak/core/contracts/context.py`
- `src/ddak/core/contracts/plan_facts.py`
- `src/ddak/core/env_keys.py`
- `src/ddak/core/project_settings.py`
- `src/ddak/core/store.py`
- `src/ddak/executor/engine.py`
- `src/ddak/executor/images.py`
- `src/ddak/executor/preparation.py`
- `src/ddak/executor/service.py`
- `src/ddak/onprem/deploy/config.py`
- `src/ddak/onprem/deploy/provider.py`
- `src/ddak/plan/analyze/logic.py`
- `src/ddak/plan/intake/watch.py`
- `src/ddak/plan/planner/logic.py`
- `src/ddak/plan/validate/__init__.py`
- `src/ddak/plan/validate/assemble.py`
- `src/ddak/web/routes/ops.py`
- `src/ddak/web/templates/_ops.html`
- `src/ddak/web/templates/ops.html`
- `src/ddak/web/templates/ops_approval.html`
