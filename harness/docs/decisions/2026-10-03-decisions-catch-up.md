# 2026-10-03 결정 따라잡기 (PR #10 이후 대화 결정 정리)

- 날짜: 2026-10-03 (KST)
- 상태: 결정 1~4, 7~11, 15, 17, 18은 확정(✅)이다. 결정 5(AI Plan·Jev)와 6(배포 순서)은 재검토·구현 중이다. 결정 12(개발값 잔존)와 13(터미널 조작 제거)은 설계를 확정했고 구현은 수정 11에서 진행 중이다. 결정 14(시연 반복 대본)는 준비 중이다. 결정 16(클라우드)은 정준우가 추후 다시 정하므로 미해결 목록만 남긴다(🟡).
- 관련 항목: [2026-10-02 팀 현황](2026-10-02-team-status-and-decisions.md)의 결정 7·9와 확인 대기, [수정 6](2026-10-02-exec-fix6.md), [수정 7](2026-10-02-exec-fix7.md), [수정 8](2026-10-03-o1-exec-fix8.md), [O1↔O2 연결](2026-10-03-o1-o2-link.md), [수정 10 UI 통합](2026-10-03-ui-integration.md)
- 결정자: 사용자(정준우, O1). AWS 계정 상태는 김준석(계정 관리자) 기준이고, `generate_infra` 구현 사실은 양서윤(C3) 커밋 기준이다.
- 기준 상태: main `971c8e6`. PR #13(수정 6·7), #11(패치 형식), #14(O1↔O2 연결), #15(수정 8·9), #17(수정 10 UI 통합과 양서윤 `cloud` 브랜치), #16(O3 smoke·compare·diagnose) merge 뒤다. 정식 툴 21개가 등록돼 있다(+ 예시 `ping`). 새로 등록된 것은 `build_image`(PR #12), `generate_infra`·`sync_env_to_cloud`(PR #17)다.
- 우선순위: 이 기록은 2026-10-02 기록들보다 늦으므로 상충하면 이 기록을 따른다. 10/2 결정 7(Terraform 분담)의 구현자 표기, 결정 9(빌드)의 "온프렘 빌드도 CodeBuild", 결정 8의 "패치 OFF 동작 확인 대기", 10/2 확인 대기 항목의 처리를 갱신한다. 같은 날의 수정 8·연결·UI 통합 기록과 다르면 이 기록을 따른다.
- 초안: 10/3 01시에 쓴 초안에 그 뒤 결정(10/3 오전·오후 대화)을 더했다.

## 맥락

PR #10(10/2 오후 문서 정리) 뒤에 사용자가 대화로 정한 결정이 저장소 문서에 없거나 수정 기록에 일부만 있었다. 수정 8~10이 LLM 설정 이름, 로컬 빌드 백엔드, `SUPERSEDED` 상태, 잠금 해제, 감시 중복 차단을 먼저 구현해서 코드가 문서보다 앞서 있었다. 그 결과 현행 문서(AGENTS.md 2절, dev-docs 00·01, roles/00_10월2일, guides/O1·O2, harness/README.md)에 이 결정과 어긋나는 문구가 남아 있었다.

10/3에는 데스크톱 WSL 실환경에서 온프렘 풀체인이 처음으로 끝까지 성공했다(결정 2). 이 기록은 결정을 한곳에 모으고, 정정할 이전 문구와 남은 미해결을 표로 남긴다.

## 결정

| # | 항목 | 결정 | 근거·구현 상태 | 상태 |
|---|---|---|---|---|
| 1 | 목표·우선순위 | 10/3 최우선은 **시연이 배포 완료까지 끝나는 것**이다. 선택지가 여럿이면 유연하게 고르되 팀원 코드와 충돌하지 않는 쪽을 고른다. 1차(기본 Flask + 이미지·박스로 파이프라인 E2E)와 2차(실제 로직·LLM 기능) 구분은 유지한다 | 10/2 결정 5를 잇는다. 1차 결과로 LLM이나 AWS 전체가 끝났다고 주장하지 않는다 | ✅ |
| 2 | 실측: 온프렘 풀체인 | 데스크톱 WSL 실환경에서 온프렘 풀체인이 성공했다. `run-20261002-175023-84a6`은 v1 첫 배포, `run-20261002-180550-2d12`는 v2 PR merge 자동 감지 → WAS만 재빌드 → 배포이며 승인 뒤 71초에 끝났다 | 정준우 실측 보고. 앱 저장소는 `2026-Gerbera/gerbera-application`(`prod`, 태그 v1 `04d779d`, v2 `adcafb9` feature/image-box)이고 deploy.yaml `tiers` 순서는 web, was다. 클라우드 실측은 아직 없다 | ✅ 사실 |
| 3 | 빌드 | 빌드 백엔드는 설정 `DDAK_BUILD_BACKEND=codebuild`(기본)\|`local`로 고른다. deploy.yaml 키는 새로 만들지 않는다. `local`은 CodeBuild와 같은 플랫폼 buildspec을 승인 SHA 사본에서 실행한다. **온프렘은 `local`을 쓴다.** 이미지 저장소는 Docker Hub 팀 저장소 `2026gerbera/flaskr`(`DDAK_IMAGE_REPOSITORY`, `ns/repo` 형식)다. 토큰 값은 문서·채팅·노션에 넣지 않는다 | 10/2 "첫 플랫폼 적용 경로" 후보 가운데 (다) 온프렘용 로컬 빌드 어댑터를 고른 것이다. `src/ddak/cloud/build/local.py`, `src/ddak/core/config.py`(`build_backend`), 온프렘 프로필 `harness/scripts/onprem_fullchain.py`(`make -C harness onprem-run`). PR #15 | ✅ |
| 4 | LLM 연결 | Groq 경로는 그대로 둔다(김준석은 Groq를 계속 쓴다). Jev 자리 판단 역할의 backend는 `DDAK_JEV_BACKEND=groq\|claude-cli`(기본 groq)로 고른다. Groq 변수는 `DDAK_GROQ_API_KEY`·`DDAK_GROQ_MODEL`·`DDAK_GROQ_TIMEOUT_S`이고, `DDAK_JEV_*`는 TypeSafe(Jev) 전용으로 예약한다. Claude CLI 판단 어댑터의 기본은 Sonnet 5.5, 추론 강도 low(`DDAK_LLM_EFFORT` low/medium)다. 관리자 페이지 AI 연결 패널(provider 등록부, CLI/API/Groq, 연결된 것만 초록 표시, API 키 입력)을 만든다. 웹에서 Claude 구독 로그인은 받지 않는다. cli backend는 본인 로컬에서만 쓴다 | `src/ddak/core/config.py`, `src/ddak/core/ai/providers/claude.py`, 온프렘 프로필은 판단·생성 모두 Claude CLI. PR #14·#15. AI 연결 패널은 수정 11에서 진행 중이다 | ✅ / 패널 진행 중 |
| 5 | AI Plan·Jev | 설계를 재검토한다. 사용자는 테스트해 보니 지금 Claude로도 충분히 성능이 나오는 것 같다고 봤다(정량 측정은 없다). 재검토에는 판단 역할을 Claude로 충분히 대신할 수 있는지와 아래 "결정 5 상세" 개편안이 들어간다. 결론이 날 때까지 김준석 구현과 Groq 경로를 유지한다 | 확인된 사실: 계획에서 AI가 정하는 것은 선택(OPTIONAL) step의 포함 여부뿐이다(`src/ddak/plan/planner/logic.py:30`). 선택 step은 `deploy.storage.<env>`(`prepare_storage`)와 `verify.watch.cloud`(`watch_post_deploy`) 두 개이고 둘 다 미등록이라 코드가 계획에서 뺀다(PR #14). 분석에서는 규칙이 바닥이고, 규칙으로 정하지 못한 환경 키만 판단 역할이 secret/plain을 고른다 | 🟡 재검토 중 |
| 6 | 배포 순서 | tier 배포 순서는 **WAS 먼저**로 한다. 사용자가 이 순서가 꼭 필요한지도 다시 검토하라고 했으므로, 검토 결과와 함께 구현한다 | 지금은 deploy.yaml `tiers` 순서(web, was)대로 tier 배포 step이 만들어진다(`src/ddak/core/contracts/step_catalog.py`의 `_env_steps`). 검토·구현은 `o1/was-first`에서 진행 중이다. 아래 "선택지와 장단점" | ✅ 방향 / 검토·구현 중 |
| 7 | 환경 트랙 격리 | 필수 환경 툴이 미등록이면 그 환경 트랙만 준비 실패로 막고 다른 환경은 승인·실행한다. 공통 빌드 툴 미등록이나 선택한 모든 환경의 준비 실패만 요청 전체를 거부한다. `generate_infra` 등 클라우드 인프라 준비 오류는 클라우드 트랙만 실패시킨다 | `RunContext.preparation_failures`(PR #14), `RunContext.preparation_errors`(수정 8 추가 5, PR #15). 둘 다 승인 컨텍스트 해시에 묶인다 | ✅ |
| 8 | 승인·잠금 | ① 승인 대기 중 같은 프로젝트·ref에 새 커밋이 오면 이전 자동 run은 `SUPERSEDED`가 된다(수동·태그 run은 보존). `SUPERSEDED` run의 승인·실행은 거부한다. ② 잠금은 쉽게 푼다: `/ops`의 잠금·차단 해제 버튼, `make -C harness unlock`, 만료(종료된 run의 만료 잠금은 다시 획득) | `src/ddak/core/store.py`(`supersede_awaiting`, `unlock_project`), `src/ddak/web/templates/ops.html`, `harness/Makefile` `unlock`. 실행 중(RUNNING) run은 해제를 거부하고, 해제는 감사 기록만 남기며 `recovery_verified=false`다. PR #15 | ✅ |
| 9 | 비밀 검사(gitleaks) | 앱 저장소 `.gitleaksignore`의 fingerprint는 원본 파일에만 적용한다. 파이프라인이 더한 부분은 엄격하게 검사한다. 검사는 승인 전(소스 사전 검사)과 push 직전(후보 전체 사본) 두 번 한다 | 정확한 fingerprint만 허용하고 wildcard는 거부한다. 승인 패치·원본과 달라진 파일은 예외 없이 전체를 재검사한다(줄 단위 정책보다 보수적). 예외 요약에는 개수·규칙·경로만 넣는다. PR #15 | ✅ |
| 10 | PEM | 공개 인증서만 든 PEM은 허용한다. 개인키 PEM과 인증서·키 혼합 PEM은 차단한다. 거부 사유와 경로를 표시한다 | 수정 9(`a605c41`, PR #15), `src/ddak/core/pem.py` | ✅ |
| 11 | 감시 대상 | 감시 대상은 저장된 프로젝트 설정 → 환경변수(`DDAK_WATCH_*`) → 기본값(flaskr / local / prod) 순서로 정한다. 같은 저장소·브랜치를 자동 감시하는 프로젝트가 둘 이상이면 저장 때 거부하고, 이미 저장된 중복은 `DDAK_WATCH_PROJECT`(기본 flaskr) 쪽 하나만 감시한다 | `src/ddak/app.py`의 `_watch_configuration`. 중복 차단은 수정 10 추가 1(PR #17). 수동 배포는 막지 않는다 | ✅ |
| 12 | 개발값 잔존(3번 문제) | 아래 "결정 12 상세". 토글 의미는 **"OFF = 새 AI 제안 없음, 패치 손실이면 멈춤"**이다. "패치 OFF면 이전 AI 패치가 빠진다"는 폐기한다 | 이미지 ref/digest 이월 일치만 수정 6·7에 있다. 나머지는 수정 11(`o1/fix11-ai-patch`)에서 구현 중이다 | ✅ 설계 / 구현 진행 중 |
| 13 | 터미널 조작 제거 | 사용자·개발자가 터미널에서 하던 설정·조작을 없앤다. 관리자 페이지, 자동 설정, config 파일로 바꾼다 | 수정 11에서 진행 중이다. 남은 수동 단계 목록은 10/2 팀 공유 요약 6-2를 기준으로 줄인다 | ✅ 방향 / 진행 중 |
| 14 | 시연 반복 대본 | 시연을 반복할 대본을 준비한다. 손으로 따라 하는 문서보다 스크립트로 만들어 반복을 쉽게 한다. 큰 이슈로 다루지 않는다 | `o1/demo-cycle`에서 준비 중이다 | ✅ 방향 / 준비 중 |
| 15 | 추후로 미루는 것 | 온프렘 블루그린 배포와 다른 AI provider(Codex 등)는 추후 검토한다. 지금 구조를 롤링 전용·Claude 전용으로 굳히지 않는다 | 10/2 결정 4(롤링, 블루그린은 후보)를 잇는다. provider는 결정 4의 등록부 구조로 붙인다 | ✅ 보류 |
| 16 | 클라우드 | 클라우드 진행 방식은 정준우가 추후 다시 정한다. 그때까지 아래 "미해결(클라우드)" 목록을 유지한다 | 10/3 01시 점검 사실: 계정 관리자는 김준석이고, 실행용 IAM 사용자 권한은 테스트 기간이라 넓게 둔다(AGENTS.md 인프라 행). 서울 리전에는 리소스가 없었다. 공개 저장소이므로 권한 정책 이름·MFA 상태 같은 계정 보안 세부와 계정 ID·키는 기록하지 않는다 | 🟡 추후 |
| 17 | `generate_infra` 구현자 | 실제 구현은 양서윤(C3)이 했고 PR #17로 main에 통합됐다. 문서의 "김준석 `generate_infra`" 표기는 실제와 다르므로 고친다. 정준우의 C1 몫(부트스트랩, `validate_infra`·`plan_infra`·`apply_infra` 실행 경로·정책)은 그대로다 | 양서윤 커밋 `3e2b678`(AWS 인프라 생성과 레지스트리 시크릿 연동), `d66b087`(클라우드 TLS 검증과 승인 표시), `486b4b1`(관리자 페이지 실배포 콘솔 개편)이 PR #17에 들어갔다. #8 계약 초안과 수정 7 출력 계약 준수는 클라우드 재개 때 확인한다 | ✅ 사실 / 🟡 계약 확인 |
| 18 | 재확인 | 트리거는 앱 저장소 `prod` PR merge이고 감시는 polling이다. 사람 개입은 승인 화면 클릭뿐이고 수동 인프라 단계는 없다. 마이그레이션 step id는 `deploy.migrate.<env>`, 온프렘 MySQL 컨테이너는 `deploy.db.local`(`deploy_tier`)이다 | 10/2 결정 1·4·6 재확인, 수정 6 구현 | ✅ |

### 결정 12 상세: 개발값 잔존

1. **파일 단위 재적용**: 이전에 승인된 패치를 새 `prod`에 파일 단위로 다시 적용한다. 원본 바이트가 이전과 같은 파일만 재적용하고, 적용 결과 해시가 이전 수정본 해시와 같은지 검증한다. 원본이 바뀐 파일만 AI가 다시 제안한다.
2. **패치 손실 관문**: 해시 비교에 더해 "이전 패치가 지운 줄이 다시 나타났는지"를 내용으로 검사한다.
3. **이월 일치 관문**: 이월하는 산출물(이미지 ref/digest 등)이 환경 장부와 일치해야 한다. 이미지 쪽은 수정 6·7에 구현돼 있다.
4. **패치 형태 제한**: 패치는 기본값 없는 필수 환경변수 읽기만 만든다. 개발용 기본값은 제안하지 않는다.
5. **정규식 관문은 보조다**: 패치로 고칠 수 있는 범주만 차단하고, 사설 IP는 경고만 한다.
6. **멈춤 단위**: 차단은 승인 전에 run 단위로 멈춘다.
7. **토글 의미**: OFF는 새 AI 제안을 만들지 않는다는 뜻이다. 이전 승인 패치를 유지할 수 없으면(패치 손실) 승인 전에 run을 멈춘다. OFF여도 이전 패치를 조용히 빼고 배포하지 않는다.

### 결정 5 상세: AI Plan·Jev 개편안(💭, 재검토 대상)

Claude가 바뀐 파일에서 근거가 있는 사실을 뽑는다 → 코드가 근거를 원문과 대조한다 → 규칙이 사실을 step으로 매핑한다 → Jev는 코드가 만든 고정 질문에만 답하고, step을 추가만 할 수 있다 → 기능별 스모크 묶음을 붙인다.

## 선택지와 장단점

### 배포 순서(결정 6, 검토 중)

| 선택지 | 장점 | 단점 |
|---|---|---|
| A. WAS 먼저(사용자 지시) | 새 web이 아직 없는 WAS 기능을 부르는 구간이 없다. 마이그레이션이 붙는 WAS를 먼저 확인하고 web을 올린다 | 옛 web이 새 WAS를 부르는 구간이 생기므로 WAS가 하위 호환이어야 한다. 순서를 deploy.yaml과 다르게 정하는 규칙이 코드에 하나 더 생긴다 |
| B. 현행(deploy.yaml `tiers` 순서, 지금은 web → was) | 바꿀 것이 없다 | 순서가 앱 저장소의 파일 순서에 따라 달라진다 |

10/3 v2 실측은 WAS만 재빌드했으므로 이 순서가 결과에 영향을 주지 않았다. 둘 다 바뀌는 배포에서 차이가 난다.

### 남은 사용자 확인: 코드 수정 토글 기본값

AGENTS.md는 "일반 실행 기본 OFF, 골든 데모 ON"이다. 수정 11 작업 트리는 설정 기본값을 ON으로 두는 안을 검토 중이다. 결정 12의 OFF 의미가 바뀌면서 ON/OFF의 위험 차이가 줄었으므로, 기본값은 수정 11 결과를 보고 정준우가 정한다.

## 미해결

### 클라우드(결정 16, 정준우가 추후 결정)

10/3 01시 점검 목록이다. PR #17(양서윤 `cloud` 브랜치) 반영 뒤 상태는 클라우드를 다시 시작할 때 다시 확인한다.

| 항목 | 점검 때 상태 | 선택지와 영향 | 확인할 사람 |
|---|---|---|---|
| 장기 키와 MFA 없는 GetSessionToken | `src/ddak/cloud/infra/assembly.py:74`가 `get_session_token`을 쓴다. MFA 없이 받은 세션 자격으로는 IAM API를 부를 수 없어서, 권한 경계를 만드는 단계가 실패할 것으로 예상한다 | (가) IAM을 호출하는 경로만 기본 자격(장기 키)을 직접 쓴다: 변경이 작지만 장기 키 노출 범위가 넓어진다 / (나) MFA를 붙인 GetSessionToken: 자격증명 준비 단계가 늘어난다 / (다) 역할 AssumeRole: 구조가 바뀐다 | 정준우·김준석 |
| BOOTSTRAP 층 구조 | 층 구성과 생성기 층 계약이 정해지지 않았다 | 생성기(양서윤 구현)의 층 출력과 C1 부트스트랩 순서를 맞춰야 한다 | 정준우·양서윤 |
| ALB 80 리다이렉트와 CKV_AWS_260 | 80→443 리다이렉트를 쓰려면 80을 열어야 하는데 이 검사가 막는다. 코드 지정 보안 그룹에만 적용하는 예외 경로가 있다(`src/ddak/cloud/infra/README.md`의 "ALB 공개 HTTP 예외" 문단) | (가) 80 리스너를 유지하고 예외로 표시한다 / (나) 80을 닫는다: http로 들어오면 접속이 실패한다 | 정준우·양서윤 |
| ECS 태스크 설정 주입 경로 | 점검 때는 비밀이 아닌 설정을 태스크에 넣는 경로가 없었다. `sync_env_to_cloud`는 PR #17로 등록됐다(Secrets Manager 값 채우기) | 비밀이 아닌 설정 경로와 dispatch 위치를 함께 정한다(수정 7의 NEEDS_CONTEXT) | 정준우·안승환 |
| TLS 정책값 불일치 | 문서는 `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09`를 요구한다. `src/ddak/cloud/tls/check.py:24`는 2021-06 두 값을, `src/ddak/cloud/health/tls.py:21-23`은 Res-PQ-2025-09와 2021-06을 허용한다 | 값을 하나로 정하고 문서·`ensure_tls`·`verify_tls`를 맞춘다 | 정준우(tls)·양서윤(verify_tls) |
| 첫 클라우드 run 순서 | platform → app 층 순서와 부트스트랩 인프라 step 강제(E6)가 정해지지 않았다 | 위 BOOTSTRAP 항목과 함께 정한다 | 정준우·안승환 |
| Docker Hub 토큰 공급(클라우드) | 온프렘은 실행 PC의 docker 로그인으로 빌드하고, 로그인 메타데이터 이상은 승인 화면 경고로만 보인다(수정 8 추가 6). 클라우드 공급은 레지스트리 시크릿 연동(`3e2b678`)을 확인해야 한다 | 클라우드 재개 때 확인한다. 값은 문서에 넣지 않는다 | 정준우·안승환·양서윤 |

### 그 밖

| 항목 | 지금 상태 | 선택지와 영향 | 확인할 사람 |
|---|---|---|---|
| 코드 수정 토글 기본값 | 위 "남은 사용자 확인" | 수정 11 결과를 보고 정한다 | 정준우 |
| 3티어 자동 계획의 DB 선행 | O1의 `deploy.db.local`은 구현됐지만 O2 자동 계획에 DB 선행 규칙이 없다 | O2 규칙에 DB 선행을 추가한다 / 그때까지는 규칙 계획을 쓴다. 결정 6(WAS 먼저)과 같이 본다 | 김준석·정준우 |
| 생성 역할 api 이름 | 계획 출처 라벨은 PR #14에서 `groq`로 바뀌었다(`src/ddak/plan/planner/logic.py`의 `_LLM_LABEL`). 남은 것은 backend 이름 `api`가 실제로는 Groq를 부른다는 점과 `Planner.provider` 주석(`src/ddak/core/contracts/plan.py`)의 `claude-api` 표기다. 10/3 O1↔O2 연결 기록의 "claude-api 라벨을 유지한다" 문구는 main 코드와 다르다 | AI 연결 패널(결정 4)의 provider 등록부에서 이름을 정한다 | 김준석·정준우 |

## 10/2 확인 대기 처리

| 10/2 항목 | 처리 |
|---|---|
| 패치 OFF run의 동작 | "OFF = 새 AI 제안 없음, 패치 손실이면 멈춤"으로 정했다(결정 12). 구현은 수정 11 |
| 블루그린 배포 | 추후 검토로 유지한다(결정 15) |
| `image_repository` 형식 | 수정 7의 `IMAGE_REPOSITORY_PATTERN`(`ns/repo`)으로 해소했다. 팀 저장소는 `2026gerbera/flaskr`(결정 3) |
| `generate_infra` 계약 | 구현자는 양서윤이고 PR #17로 통합됐다. 계약 준수 확인은 클라우드 재개 때(결정 16·17) |
| 첫 플랫폼 적용 경로 | 온프렘은 로컬 빌드 백엔드로 AWS 플랫폼 없이 진행한다(결정 3). 클라우드 첫 run 순서는 미해결(클라우드) |
| Docker Hub 토큰 공급 방식 | 저장소는 확정했다. 온프렘은 실행 PC 로그인, 클라우드는 미해결(클라우드). 값은 문서에 넣지 않는다 |

10/2 기록의 그 밖의 항목(G9c 코드 안 기본값 키, G9d 새 앱 DB, E6 담당, 클라우드 검증 출력 키)은 이 기록에서 처리하지 않는다. 클라우드 쪽은 결정 16에서 같이 다시 본다.

## 대체되는 이전 문구

| 이전 문구 | 위치 | 현재 기준 | 이번 처리 |
|---|---|---|---|
| 패치 OFF면 이전 AI 패치가 빠진다(확인 대기) | AGENTS.md 2절 코드 수정 토글 행, guides/O1.md 첫 절, roles/00_10월2일 1-8, dev-docs/01:360·805, 10-02 team-status 결정 8·확인 대기 | 결정 12 | AGENTS.md·O1·roles/00_10월2일 정정, dev-docs 00·01 상단 배너, 10/2 기록은 그대로 |
| 이전에 승인된 같은 패치는 자동 재사용 | dev-docs/00:19, dev-docs/01:14 | 파일 단위 재적용 조건(결정 12) | dev-docs 상단 배너 |
| 환경변수 이름과 개발용 기본값을 제안 | dev-docs/01, roles/O3 | 기본값 없는 필수 env 읽기(결정 12) | dev-docs 상단 배너. roles/O3는 장민영 확인 뒤 수정 11과 함께 |
| Groq = `DDAK_JEV_API_KEY`/`DDAK_JEV_MODEL` | roles/00_10월2일 4-4-1 | `DDAK_GROQ_*`(결정 4) | 정정. guides/O2.md는 PR #14에서 이미 정정 |
| 온프렘 빌드도 CodeBuild | 10-02 team-status 결정 9, roles/00_10월2일 1-9·4-3·4-4·5절, harness/README.md 구조도 | 로컬 빌드 백엔드(결정 3) | roles/00_10월2일·README 정정, 10/2 기록은 그대로 |
| Terraform 본문은 김준석 `generate_infra` | AGENTS.md 머리말·디렉토리 트리, guides/O1.md 팀별 연결점, docs/harness/01 디렉토리 표, roles/00_10월2일 1-7·5절, dev-docs/README 역할 표, dev-docs/02 C1 분담, dev-docs/00 디렉토리·역할 표, dev-docs/01 카탈로그·C-17·C-20, roles/O1·O2·O3·C2, handoff/C1 제목, `src/ddak/cloud/infra/README.md:1`, 10-02 team-status 결정 7 | 양서윤 구현(결정 17) | AGENTS.md·O1·docs/harness/01·roles/00_10월2일·dev-docs/README·dev-docs/02·cloud/infra README 정정, dev-docs 00·01 상단 배너. 개인 역할 문서(roles/O1·O2·O3·C2)와 handoff/C1은 그대로 두고 이 표로 대체 |
| TTL 만료만으로 잠금을 빼앗지 않는다 / 강제 해제 미제공 | guides/O1.md "로컬과 기록"·"10/1 독립 검토 반영", dev-docs/01:785 | 버튼, `make unlock`, 만료(결정 8) | O1 정정, dev-docs 상단 배너 |
| `prod`에 새 커밋이 와도 이 run은 그대로 진행 | dev-docs/01:712 | 승인 대기 중이면 `SUPERSEDED`(결정 8) | dev-docs 상단 배너 |
| 등록 툴 18개, `build_image` 미등록 | 10-02 team-status 맥락, roles/00_10월2일 0절·4-1 | 정식 21개(기준 상태) | roles/00_10월2일 상단 배너 |
| `local_verified` 대기 지점, 로컬 검증 지점에서 기다림 | harness/README.md 구조도·파이프라인 설명·디렉토리 표 | 환경 간 대기 없음(10/1 밤) | 정정 |
| PR마다 "AI 사용" 칸 | harness/README.md AI 사용 절 | PR 템플릿 "변경 내용"에 기록 위치(10/2 결정 10) | 정정 |
| 단계 안 step은 AI가 넣고 뺀다 | AGENTS.md 2절 계획 행 | AI는 선택 step 포함 여부만 제안, 미등록 선택 step은 코드가 뺌(결정 5·7) | 정정 |
| `watch.py`가 `main`을 감시 | `src/ddak/plan/intake/README.md:38` | `prod`(결정 18) | 김준석 요청 |

## 반대 의견

대화로 정한 결정이라 반대 의견 기록이 없다. 김준석(결정 4·5), 양서윤(결정 17), 안승환(결정 3), 장민영(결정 12)의 의견을 확인한 뒤 여기에 추가하고, 지우지 않는다.

## 되돌리는 조건

- 결정 5: 재검토 전이라도 리허설에서 현행 계획 생성이 시연 경로를 막으면 개편안을 앞당긴다.
- 결정 4: claude-cli나 Groq의 지연·실패율 때문에 시연이 멈추면 replay 비상 경로를 쓰고 backend를 다시 정한다.
- 결정 3: `local`과 CodeBuild 산출물의 플랫폼·digest 구성이 다르게 나오면 다시 본다.
- 결정 6: 검토에서 WAS 먼저가 필요 없다고 확인되면(하위 호환 문제가 없고 실익이 없으면) 현행 순서를 유지할 수 있다. 정준우가 정한다.
- 결정 12: 데모 앱에서 파일 단위 재적용이 계속 재제안으로 빠지면 단위를 다시 본다.

## 영향

- 정정한 문서: `harness/AGENTS.md`(머리말·1절 포인터, 2절 계획·LLM·이미지·코드 수정 토글 행, 디렉토리 트리의 `generate_infra` 담당, 최신 실행 전제), `harness/README.md`, `harness/docs/guides/O1.md`, `harness/docs/guides/O2.md`(상단 정정 표시와 최소 정정), `harness/docs/harness/01_저장소-구조와-소유권.md`(`generate_infra` 담당 2줄), `single-app/dev-docs/roles/00_10월2일-변경사항.md`(상단 배너와 해당 줄), `single-app/dev-docs/00_공통-개요.md`·`01_공통-계약.md`(상단 배너만), `single-app/dev-docs/02_디렉토리-소유.md`·`README.md`(`generate_infra` 담당 표기), `src/ddak/cloud/infra/README.md`(제목의 담당 표기).
- 지난 날짜 결정 기록과 인계서는 본문을 고치지 않는다.
- 다른 담당 README(장민영·안승환·양서윤 소유)와 김준석 `plan/intake/README.md`는 고치지 않고 담당자에게 요청한다.
- 계약: 이 기록은 툴 이름·파라미터·스키마·이벤트·카탈로그 메타정보·deploy.yaml 키를 바꾸지 않는다. 결정 6(배포 순서)과 12(패치)의 구현이 공유 계약을 바꾸면 해당 작업에서 `NEEDS_CONTEXT`로 올린다.
- 구현 상태 표기: 수정 11·`o1/was-first`·`o1/demo-cycle`의 내용은 merge 전까지 "진행 중"으로 쓴다. "main 구현 완료"로 쓰지 않는다.
