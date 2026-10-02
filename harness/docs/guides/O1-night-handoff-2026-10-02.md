# 10/2 인계 — 정준우 O1·C1 (수정 라운드 반영)

> **최신 작업 G:** [E2E 연결 인계](O1-G-handoff-2026-10-02.md). 아래 내용은 이전 야간 작업 기록이다.

> **후속 수정 2:** 아래 3개 커밋은 정준우가 이미 반영했다. 이 문서는 그 시점의 인계 기록이며,
> 새 후속 수정은 [O1 작업 기록](../ai-usage/O1.md)의 후속 수정 2와 최신 결정 §9를 따른다.

당시 `o1/onprem-three-tier` 미커밋 작업 트리가 결과였다. stage·commit·push·PR 없음.
VM 시험 완료 후 VM/SSH 재접속, 실제 AWS·Terraform apply/destroy·원격 Git·Docker Hub push 없음.
로컬 bare Git 시험 저장소에만 후보 commit/push를 실행했다. TDD 없이 구현 후 회귀 테스트와 독립 검토를 했다.

## 현재 완료 범위

| 야간 항목 | 상태 | 구현 | 남은 연결 |
|---|---|---|---|
| 1. 대기 지점 제거 | DONE | 독립 트랙·실행한 tier만 복구·양쪽 성공 시 compare | 구형 계획 재생성 |
| 2. 대상·trigger | DONE | onprem/cloud/both·N/A·manual/auto, targets=None이면 계획 유지 | 요청 접수·화면에서 기본 대상 지정 |
| 3. Git 기록 | DONE(로컬 bare 검증) | source/candidate SHA·성공 환경 태그·선택 전체 성공 때 main FF | 앱 저장소 매핑·권한·실 원격 검증 |
| 4. ai-prod 후보 | DONE(로컬 bare 검증) | 승인 트리 확정·이력 보존 merge·비밀검사·고정 SHA 일반 push | O3 재사용 적합성 판단·C2 sourceVersion |
| 5. WP11 | 부분 | 공개 Python 조회·버전 설정 API·실제 저장값 승인 스냅샷·C-18 전달 | O2 감시→prepare·C3 HTTP/화면 |
| 6. C1 | 부분 | 기반 Terraform 파일·출력 허용목록·인프라 툴/refresh·443 검사 | 생성기 계약·실 세션·제품 승인 기반 foundation/platform apply·실 AWS 검증 |
| 7. 문서 | DONE | 가이드·결정·수정 기록·벤치마크·3개 커밋 계획 | 아래 담당자/사람 행동 |

## 수정 라운드 결과

| 번호 | 상태 | 변경·검증 요점 |
|---|---|---|
| 1 | DONE | 승인 build_files로 관리 파일 덮어쓰기/삭제, 충돌 목록 기록, 두 이력 부모 유지. 반복 run 4종 및 ignored 승인 파일 회귀 |
| 2 | DONE | .env.example/.env.sample은 prod 내용 유지·승인 비교 제외·비밀검사 포함. .env/PEM/KEY 차단 회귀 |
| 3 | DONE | 실제 deploy_tier/prepare_db 호출 tier만 rollback. 빌드 실패는 FAILED_BEFORE_DEPLOY, 배포 미시작 환경 SKIPPED. infra_changes 별도 보존, refresh 실패도 적용 성공 기록 유지 |
| 4 | DONE | compare/diagnose 실패만 PARITY_FAILED. 환경별 verify 실패는 해당 환경 복구, 반대 환경 검증 계속. compare 건너뛰기는 STEP_SKIPPED |
| 5 | DONE | 443 DNS~HEAD probe min(10초, 남은 시간), 자식 종료·회수. 시간 초과 False. registry 메타 유지 |
| 6 | DONE | targets=None은 계획 유지, 설정 기본값으로 축소/거부하지 않음. 승인 스냅샷은 실제 저장 필드만 |
| 7 | DONE | store의 일부 키 저장은 기존 값과 원자적 병합. legacy 웹 3키 저장의 데이터 손실 방지 |
| 8 | DONE | 문서·요청 목록·watch 소유자·fixture 상태표 정정, 공개 산출물의 quick tunnel 주소 가림 |
| 9 | DONE(보류 기록) | 지시한 나머지 P3는 구현하지 않고 아래에 남김 |

패치 OFF는 승인 트리가 prod 그대로이므로 이전 AI 패치가 빠진다. **정준우 확인 필요**로 결정에 기록했다.
merge 충돌은 실행 중 재패치 요청으로 중단하지 않는다. O3가 승인 전에 재사용 패치의 적합성을 판단한다.
양쪽 배포 실패의 대표 상태는 FAILED_LOCAL이고 각 tracks에 두 결과를 남긴다. 복구 실패/종료 불명은 NEEDS_HUMAN이 우선한다.
공통 비동등성 검증 실패는 FAILED_VERIFY이며 C3 표시 연결이 필요하다.

## 이미 확인한 실환경과 한계

**야간 변경 전** WSL→VM 3대/WAS 3복제본: WAS 54.161초, 3티어 첫 배포 101.377초,
v2 75.195초, 실패+복구 96.274초(복구 26.875초), DB 보존 v1 복귀 73.128초→v2 72.778초.
HTTP 565표본 오류 0. [원본 조건·CSV/JSON](../benchmarks/2026-10-02-onprem-vm/README.md).
3티어 reset_demo는 거부됐으며 DB 삭제 초기화는 미지원이다. DB 보존 재배포와 구분한다.
공개 보고서/최종 assertions의 임시 터널 주소는 `<quick-tunnel-url>`로 가렸다. 측정값은 바꾸지 않았다.

야간·수정 코드의 검증은 단위/서비스 fixture·로컬 bare Git이다. 실제 AWS·CodeBuild·팀 앱 로그인·
C3 화면·사람 승인 포함 3분 E2E는 미검증이며 전체 데모 완료로 발표할 수 없다.
로컬 Gitleaks 8.30.1 실제 CLI는 안전 파일·가짜 토큰 fixture와 후보 경로에서 확인했다.
**원격 CI의 quality job에는 Gitleaks 설치가 없어 실제 CLI 테스트 2개가 skip된다.** 별도 secrets job은
Gitleaks를 설치해 Git 변경을 검사한다. 그 job이 후보 생성의 실제 CLI 테스트까지 대체하지는 않는다.
원격 CI는 이번 작업에서 실행하지 않았다.

## 제안 커밋 순서 — 파일 단위 3개

같은 파일의 여러 야간 항목을 쪼개지 않는다. `git add -p` 없이 아래 전체 파일 묶음으로 검토한다.
스키마·실행기·C1·설정은 서로 의존하므로 **2번에 함께** 넣는다. 에이전트는 stage하지 않았다.

| 순서 | 파일 묶음 | 제안 커밋 메시지 |
|---|---|---|
| 1 | VM 검증용 인벤토리 정합화: `src/ddak/onprem/deploy/__init__.py`, `src/ddak/onprem/inventory/__init__.py`, `harness/tests/unit/onprem/inventory/test_inventory.py` | `온프렘 3티어 인벤토리 모델과 provider 필드를 정합화` |
| 2 | 나머지 변경/신규 `src/` 생산 코드(README 제외), `harness/tests/`(1번 파일 제외), `harness/contracts/schemas/`, `harness/fixtures/plans/golden_v2_update.json`, `harness/scripts/o1_demo.py`, foundation `.tf` | `독립 배포 트랙과 승인 후보 및 C1 실행 경로 구현` |
| 3 | 변경/신규 Markdown 전체(코드 디렉토리 README 포함), `harness/docs/benchmarks/`의 JSON·CSV·patch 등 공개 산출물 | `VM 벤치마크와 야간 수정 결과 및 팀 인계 절차 기록` |

1번에는 VM 검증을 위해 고친 코드·테스트만, 실제 VM 보고/측정 파일은 3번에 넣는다.
2번에는 신규 infra/TLS 입출력 스키마와 기존 plan/validate_plan 스키마를 반드시 같이 넣는다.
각 파일이 한 묶음에만 속하도록 [파일 목록](../benchmarks/2026-10-02-corrections/commit-files.json)에 보존한다.
`harness/var/validation/` 원본 로그·개인 환경·실행 상태·비밀 파일은 커밋 대상이 아니다.

## 담당자 요청 목록

| 담당 | 우선 전달할 내용 |
|---|---|
| **서윤 — 우선** | **웹 설정 저장을 공개 API `save_project_settings(..., expected_version=...)`로 교체.** store 일부 키 병합은 데이터 손실 방어만 하며 stale 화면 버전 검증을 대신하지 않음. 조회도 service 공개 API로 교체 |
| 준석 | onprem/inventory 모델을 provider 기준으로 맞춘 변경 확인. **기존 watch.py standalone 기본 main→prod 변경은 준석님 몫**. 앱 조립부는 저장된 project_settings의 watch_branch를 Watcher에 공급 중이며, auto_detect·targets/trigger/source_sha·인벤토리 취합을 확인, default_targets는 접수 시만 사용, 변경 탐지는 마지막 성공 source_sha |
| 준석(야간 최소 수정) | `plan/validate/assemble.py`의 has_local 조건부 local_verified 제거만 수정. SignalName 유지. 구형 wait 계획 재생성 필요 |
| 준석(C1) | generate_infra C-20 파일/변수/출력 주소 계약, 초기 foundation/platform은 한 화면 infra 승인, 앱 개선 배포는 별도 run, 실 세션·영속 runtime·Analyzer·bind_infra 조립. 첫 플랫폼은 app 조립이 apply→infra_ready→build/TLS 순서를 승인 전에 구성. 생성기 binding 통합 필요 |
| 민영 | **재사용 패치가 새 prod에 안 맞으면 승인 전에 재제안.** merge 충돌 자체는 실행기를 멈추지 않음. 앱 저장소 .env.example/.env.sample 허용·.env 금지, 템플릿 비밀검사 포함. smoke/compare 구현과 💭 v2 후보 시나리오 연결(로그인 미확정) |
| 승환 | **CodeBuild sourceVersion=candidate_sha**, 실제 빌드한 커밋 SHA 결과 반환·이미지 revision 라벨. S3 VersionId 대체 경로와 구분. **공유 `cd/dispatch.py` TLS 콜백 주입 변경 공지**, C2 provider 직접 ensure_tls는 미구현. ECS/시크릿/DB·복구·index/platform digest 연결 |
| 서윤(화면) | N/A·FAILED_VERIFY·infra_changes·STEP_SKIPPED와 환경별 결과 표시, targets/trigger·C-18/patch_meta·별도 Git 게시 실패 표시. 인증/CSRF/이스케이프 경계 유지. 앱 성공/HSTS 검증 취합 |
| 공유 검토 | ensure_tls registry의 STATE_CHANGE/lock/2700초 메타는 유지했음. 확인 전용 의미로 정리할지 합의 필요. preflight/reset ops 등록·3티어 DB 초기화는 범위 밖 |

## 남은 P3 — 이번에는 구현하지 않음

- Git worktree 정리 정책, prod 이력 재작성 처리.
- Terraform 출력 list(string) 원소 검증, foundation/생성 번들의 권한 경계 일치 테스트.

## 사람이 진행할 일

💭 v2 기능은 미정이며 로그인은 후보입니다. 정준우의 10/2 검증은 1차 Flask 기본 앱에 이미지·박스를 추가한 파이프라인 E2E, 2차 실제 로직의 LLM 분석·패치·인프라 생성 검증으로 나눕니다. 1차 결과로 실제 LLM·AWS 전체 완료를 주장하지 않습니다.

1. 위 3개 묶음 diff를 검토·커밋하고 기존 PR을 갱신한다. 패치 OFF의 이전 패치 제거 동작을 확인한다.
2. 요청 목록을 담당자에게 전달한다. 에이전트는 팀원에게 메시지를 보내지 않았다.
3. 프로젝트 설정에 앱 `repo_url`·감시 브랜치를 저장한다. `ddak.app`이 전용 checkout/factory를 연결하며 수동 repositories 매핑은 필요 없다. 승인 URL과 실제 origin fetch/push URL이 다르면 거부한다. Gitleaks·실행 사용자 Git 신원/권한·브랜치 보호는 실행 호스트에서 준비한다.
4. 사람은 AWS 자격증명과 도메인 구매를 준비한다. foundation/platform 사람 사전 apply 전제는 폐기한다.
   [제품 실행 기준](../../../src/ddak/cloud/infra/terraform/foundation/README.md)에 따라 validate→plan→한 화면 infra 승인 뒤 코드가 foundation/platform을 적용한다. bucket이 없으면 승인된 local plan→SDK bucket 생성→platform local apply→remote backend state 이전이다. 실제 생성기 O2 연결·실 AWS 검증은 미완이다.
5. 팀 연결 후 새 승인으로 온프렘만→클라우드만→양쪽을 검증하고 실패/복구/전체 시간을 재측정한다.
   기존 VM 시간을 수정 후 전체 파이프라인 시간으로 재사용하지 않는다.

## 마감 검증

최종 `make -C harness ci`: **1,035 passed, 1 skipped, 4 deselected / 56.495초**(pytest 54.25초).
lint/type/import 경계/계약 모두 PASS. 독립 검토에서 문서의 구형 패치 유지·충돌 중단 문구 1건을 찾아 정정했다.
코드 1–7과 문서 8–9의 독립 검토를 마쳤고 확인된 지적을 모두 반영했다.
파일 목록 108개(1번 3개/2번 67개/3번 38개)의 중복·누락 없음, stage 없음, URL 가림·JSON 파싱·`git diff --check`를 확인했다.
1차 CI는 1,033 passed/1 skipped/4 deselected였으나 lint 4건 때문에 실패했고 교정했다.
독립 검토에서 확인된 승인 파일의 gitignore 누락, refresh 실패 시 적용 기록 누락,
한 환경 verify 실패 시 반대 환경 검증 중단/compare skip 기록 누락도 회귀 테스트와 함께 교정했다.
[수정 벤치마크](../benchmarks/2026-10-02-corrections/README.md)에 UTC·소요 시간·로그 해시를 기록한다.
원본 성공/실패 로그는 `harness/var/validation/correction-20261002/`에 보존한다.
