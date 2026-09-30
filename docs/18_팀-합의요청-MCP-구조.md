# 18. 팀 합의 요청: MCP 서버 대신 "앱 1개 + 툴 레지스트리" (🟡 2026-09-30)

> 상태: 정준우 제안, **팀원 동의 후 확정**. 회신을 받으면 [02](02_의사결정-로그.md)에 기록하고 하네스([16](16_병렬-개발-규칙과-하네스.md))와 역할별 가이드에 반영합니다.
> 실행 방식(호스트 실행 + 컨테이너 이미지 둘 다)은 확정입니다(✅ 2026-09-30).

---

## A. 채팅방에 바로 붙일 짧은 버전

> **[합의 요청] MCP 서버 3개 → "앱 1개 + 툴 레지스트리"로 바꾸는 안** (회신: 〈마감 시각〉까지)
>
> **왜:** 우리 구조에서는 실행 중에 LLM이 툴을 고르지 않고 실행기 코드가 호출합니다. 툴을 쓰는 곳도 우리 앱 하나뿐입니다. MCP 서버를 3개 따로 띄우면 이점은 거의 없고 프로세스·통신·디버깅 부담만 늘어납니다. 외부 GPT 검토도 "이 규모면 파이썬 인터페이스로 충분하다"고 봤습니다.
>
> **제안 구조**
> - 관리 웹 + 계획 + 계획 검증 + 실행기 + 툴을 **파이썬 앱 1개(한 프로세스)**로 만듭니다.
> - 툴은 코드 안 **레지스트리**로 둡니다. LangChain `@tool`처럼 데코레이터로 함수를 등록하고, 입력·출력 스키마(pydantic)와 메타정보(단계, AI 사용 여부, 필수 step 여부, 담당)를 붙입니다. LangChain 패키지는 쓰지 않습니다.
> - 모듈은 3개입니다: 계획·검증 / 빌드·배포 / 검증·보고. 담당 경계는 그대로입니다.
> - MCP는 필요해지면 나중에 얇은 어댑터로 붙입니다(선택).
> - 권한 분리: 한 프로세스로 합치면 웹 화면과 docker·AWS·LLM 자격증명이 한곳에 모입니다. 호스트 실행(데모)은 `127.0.0.1`에만 열고, 컨테이너 이미지에서는 같은 코드를 **웹(관리 화면·계획) / 실행기(배포 툴, docker·AWS 권한)** 두 컨테이너로 나눠 띄우는 안을 함께 검토합니다.
>
> **동작 방식: AI는 JSON만 만들고, 실행은 항상 코드가 합니다**
> 1. 채팅 "배포해줘" → AI가 의도 JSON 생성: `{"action":"deploy_request","target":"both"}` → 코드가 허용 목록 확인
> 2. 분석(규칙 탐지 + AI 분류) → AI가 **단계 안 step을 넣고 빼서** 계획 JSON(plan.json) 생성. 단계 순서(빌드 → 배포 → 검증)는 고정입니다
> 3. 계획 검증(코드): 목록에 없는 step, 필수 step 누락, 허용 안 된 파라미터 → 불합격이면 재지시 1회 → 규칙 계획
> 4. 사람이 "배포" 클릭 → **실행기가 plan.json 순서대로 레지스트리 함수를 직접 호출**(AI 없음). 이미지 digest·도메인·시크릿 위치는 AI가 아니라 실행 컨텍스트에서 코드가 채웁니다
> 5. 로컬·클라우드는 **동시에 시작해 병렬로** 갑니다. 로컬 검증이 필요한 지점(예: 클라우드 DB 마이그레이션·배포)에서만 `wait_for`로 대기합니다. 로컬이 실패하면 클라우드는 그 지점에서 멈춥니다
> 6. 실패하면 해당 환경만 규칙 롤백 → AI가 원인 설명 → 보고. 코드 수정 토글은 기본 OFF라 보고까지만 합니다
>
> **LLM 호출:** `call_ai` 하나로 통일하고 호출 방식만 설정으로 바꿉니다. 개발은 로그인해 둔 Claude CLI(`claude -p`), 데모는 Anthropic API, 테스트·비상용은 저장된 응답 재생입니다. 모델은 툴을 직접 호출하지 않고 정해진 형식의 JSON만 돌려주므로 CLI와 API에서 똑같이 동작합니다.
>
> **실행 방식(확정):** 개발·데모는 호스트에서 `uv run`, 배포용으로 컨테이너 이미지 1개(`docker run` 한 줄)도 만듭니다.
>
> **안 바뀌는 것:** 역할 분담, 파이프라인 5단계, "실행 경로에 AI 없음" 원칙, 툴 이름과 입력·출력 계약.
>
> **답해 주세요**
> 1. MCP 서버 없이 "앱 1개 + 레지스트리"로 가는 데 동의하나요? (동의 / 반대 / 조건부)
> 2. MCP 서버를 꼭 남겨야 할 이유가 있나요? (발표·평가·기술 관점 모두)
> 3. "AI는 JSON(의도·계획)만 만들고 실행은 항상 코드가 한다"는 원칙을 관리 페이지 채팅까지 적용하는 데 동의하나요?
> 4. 본인 담당 툴을 아래 모듈 배치(B-3)에 두는 데 문제가 있나요?
> 5. 컨테이너 배포 때 웹 / 실행기 두 컨테이너로 권한을 나누는 안에 의견이 있나요?
> 6. LLM 호출 방식(개발 CLI / 데모 API)에 의견이 있나요? API 키 비용·관리 방법도 같이 정해야 합니다
> 7. 기타 우려
>
> 자세한 설명: docs/18_팀-합의요청-MCP-구조.md (Notion에 옮길 예정)

---

## B. 자세한 설명 (Notion용)

### B-1. 지금 구조와 문제

| 항목 | 지금(MCP 서버 3개) | 우리 실제 사용 방식 |
|---|---|---|
| 툴을 부르는 주체 | MCP 클라이언트 | 실행기 코드. LLM은 실행 중 툴을 고르지 않음(AI 경계) |
| 툴을 쓰는 곳 | 여러 AI 클라이언트가 재사용 가능 | 우리 앱 하나 |
| 프로세스 | 서버 3개 + 실행기 + 관리 페이지 | 하나로 충분 |
| 보안 경계 | 서버마다 자격증명을 나누면 경계가 됨 | 한 자격증명으로 돌리면 경계가 아님(외부 GPT 검토에서도 지적) |

- MCP의 핵심 가치는 "여러 AI 클라이언트가 같은 툴을 표준 방식으로 쓰는 것"입니다. 우리 구조에서는 이 가치를 쓰지 않습니다.
- 서버를 따로 띄우면 전송 방식(stdio/HTTP), 직렬화, 서버 기동 순서, 서버 사이 오류 추적이 추가됩니다. 3일 안에 통합하다 터질 지점이 늘어납니다.
- 심사위원 예상 질문 "LLM이 안 부르는데 MCP는 왜 쓰나요?"에 대한 답도 약합니다.

### B-2. 제안 구조

```
파이썬 앱 1개 (한 프로세스)
├── 관리 웹 (채팅, 계획, 진행, 결과, 설정)
├── 계획: LLM이 step 목록을 보고 단계 안 step을 선택 → 계획 JSON
├── 계획 검증: 필수 step 누락, 목록에 없는 step, 토글 검사 (코드)
├── 실행기: 계획 순서대로 레지스트리 함수를 직접 호출 (AI 없음)
└── 툴 레지스트리
    ├── plan_validate/  (계획·검증)
    ├── build_deploy/   (빌드·배포, local/cloud 어댑터)
    └── verify_report/  (검증·보고)
```

- **툴 하나 =** 파이썬 함수 + 입력·출력 스키마(pydantic) + 메타정보(단계, AI 사용 여부, 담당, 필수/선택 step 여부) + local/cloud 어댑터.
- **"AI 없음" 강제:** LLM 호출 모듈은 허용된 툴(분석·계획·패치·원인 분석·보고 요약)만 import할 수 있게 코드 규칙(import-linter)으로 검사합니다. 한 프로세스라서 오히려 더 쉽게 강제할 수 있습니다.
- **채팅 AI가 툴을 쓸 때:** 조회용 툴과 "배포 요청(사람 승인 필요)"만 허용합니다. 빌드·배포 툴은 직접 호출하지 못합니다.
- **MCP 어댑터(선택, P2):** 레지스트리에 스키마가 있으므로 나중에 MCP 서버 형태로 노출하는 얇은 층을 수십 줄로 붙일 수 있습니다.

### B-3. 모듈별 툴 배치 (초안)

골든 패스에 꼭 필요한 툴을 먼저 개발합니다. P0 목록은 [17](17_골든패스-시나리오.md)에서 확정합니다.

| 모듈 | 툴 | 담당(현재) |
|---|---|---|
| `plan_validate` (계획·검증) | receive_deploy_request, analyze_project, detect_changed_tiers, generate_plan, validate_plan, (토글) patch_db_access · patch_storage · patch_config | 김준석(O2), 패치는 장민영(O3) |
| `build_deploy` (빌드·배포) | build_image, acquire_deploy_lock, ensure_infra, inject_env_config, sync_env_to_cloud, prepare_db, prepare_storage, ensure_tls, deploy_tier, rollback_tier | 클라우드 어댑터 안승환(C2) · 유상준(C1: ensure_tls·인프라), 온프렘 어댑터 정준우(O1) · 김준석(인프라 프로비저닝) |
| `verify_report` (검증·보고) | health_check, smoke_test, verify_tls, compare_env_results, watch_post_deploy, collect_diagnostics, diagnose_parity_gap, record_deploy_log, post_report | 양서윤(C3: 클라우드 검증), 장민영(O3: 교차 검증·원인 분석) |
| 공용 코어 | call_ai, request_approval, stream_progress | 김준석(O2: call_ai), 정준우(O1: 승인·진행 이벤트), 양서윤(C3: 관리 웹 화면) |
| 운영 | preflight_check, reset_demo_state, cleanup | 정준우(O1) · 유상준(C1) |

### B-4. 잃는 것과 대응

| 잃는 것 | 대응 |
|---|---|
| 발표에서 "MCP" 키워드 | AI 점수는 MCP 사용 여부가 아니라 AI가 어려운 일을 하는지로 매겨집니다. 필요하면 MCP 어댑터(P2)로 "MCP 호환"을 보여줄 수 있습니다 |
| 다른 AI 클라이언트의 툴 재사용 | 지금 범위에 없습니다. 어댑터로 나중에 가능합니다 |
| 프로세스 분리로 얻는 격리 | 지금도 같은 자격증명이면 실질적인 격리가 아니었습니다. 권한 분리는 IAM(CodeBuild 빌드 역할 ≠ 배포 역할)과 계획 검증으로 합니다 |

### B-5. 실행 방식 (✅ 확정)

| 방식 | 용도 | 이유 |
|---|---|---|
| 호스트 실행 (`uv run` / `make up`) | 개발·데모 | Claude CLI 로그인, 호스트 docker, `~/.aws`를 그대로 사용 |
| 컨테이너 이미지 1개 (`docker run` 한 줄, WebGoat 방식) | 배포·공유 | 한 줄로 구축. 컨테이너 안 docker 접근·자격증명·Claude 인증 방식은 별도로 정리(조사 중) |

### B-7. 실행 흐름 상세 (의도 JSON → 계획 → 실행)

```
[채팅] "로그인 기능 배포해줘"
   │ AI → {"action":"deploy_request","target":"both"}        ← AI (의도 JSON)
   ▼
[코드] 허용 목록 확인 → receive_deploy_request → run_id 발급
   ▼
① 분석: 규칙 스캐너 탐지 → AI 분류 (새 설정 키, 마이그레이션 유무 등)
① 계획: AI가 단계별 step 선택 → plan.json                   ← AI (계획 JSON)
   ▼
② 계획 검증 (코드) → [관리 페이지] 계획 표시 → 사람이 "배포" 클릭
   ▼
③④⑤ 실행기 (코드, AI 없음): 레지스트리 함수를 직접 호출
```

plan.json 예시(요약):

```json
{
  "run_id": "run-0930-01",
  "build":  {"steps":   [{"step": "build_image", "tier": "was"}],
             "skipped": [{"step": "build_image", "tier": "web", "reason": "web 변경 없음"}]},
  "deploy": {
    "local": [{"step": "inject_env_config"},
              {"step": "db_migrate", "migration": "002_add_users"},
              {"step": "deploy_tier", "tier": "was"},
              {"step": "smoke_test", "signal": "local_verified"}],
    "cloud": [{"step": "sync_env_to_cloud", "reason": "SECRET_KEY 신규"},
              {"step": "db_migrate", "migration": "002_add_users", "wait_for": "local_verified"},
              {"step": "deploy_tier", "tier": "was", "wait_for": "local_verified"},
              {"step": "smoke_test"},
              {"step": "verify_tls"}]
  },
  "verify": {"steps": [{"step": "compare_env_results"}]}
}
```

실행기 골격:

```python
async def run_track(track, ctx):
    for step in track.steps:
        if step.wait_for:
            await ctx.gate(step.wait_for).wait()                 # 로컬 검증 대기 지점
        t = REGISTRY[step.step]                                  # 검증 통과한 이름만 존재
        inp = t.input_model(**step.params, target=track.target)  # 스키마로 파라미터 재검증
        try:
            out = await asyncio.wait_for(t.fn(inp, ctx), timeout=t.timeout)
        except Exception:
            await ctx.rollback(track.target)                     # 규칙 롤백: 이 환경만
            ctx.fail_gates(track.target)                         # 로컬 실패 → 클라우드 중단
            raise
        ctx.record(step, out)
        if step.signal:
            ctx.gate(step.signal).set()                          # 로컬 스모크 통과 → 클라우드 진행
```

- 필수 step(잠금, 헬스, 스모크, 클라우드 TLS 검증 등)은 AI가 뺄 수 없습니다. 계획 검증이 막습니다.
- 정식 형식(plan.json 스키마, 레지스트리 메타정보, 대기 지점 규칙)은 공통 계약 문서에서 확정합니다. 실행기는 정준우(O1), 계획·계획 검증은 김준석(O2) 담당입니다.

### B-8. LLM 호출 방식

| backend | 용도 | 방식 |
|---|---|---|
| `cli` | 개발 | `claude -p --tools "" --json-schema …` (로그인해 둔 CLI, 도구 전부 끔, 쉘 없이 인자 배열로 실행) |
| `api` | 데모 | Anthropic API (API 키) |
| `replay` | 테스트·비상 | 저장된 응답 재생(화면에 "저장된 응답" 표시) |

- 가림·타임아웃·재시도·스키마 검증·비용 기록은 `call_ai` 안에서 공통으로 처리합니다. 그래서 backend를 바꿔도 앱 동작이 같습니다.
- 모델은 툴을 직접 호출하지 않습니다. CLI는 우리 파이썬 함수를 알 수 없으므로, 두 방식에서 똑같이 되는 "JSON만 반환" 방식으로 통일합니다.
- 약관 확인 결과(research/2026-09-30_웹-CLI-LLM호출-검토.md): 구독 CLI는 **개발자 각자 로컬에서 본인만** 쓰는 용도로 한정합니다. 관리 페이지를 외부에 열어 타인이 채팅하거나, 구독 토큰을 서버·ECS에 넣거나, 웹에서 Claude 로그인을 받는 것은 Anthropic이 금지하는 패턴입니다. 데모는 API 키로 갑니다.

### B-6. 동의하면 바뀌는 문서
- [16](16_병렬-개발-규칙과-하네스.md) 하네스: MCP 서버 진입점·MCP 호출 테스트 → 레지스트리·직접 호출 테스트
- [17](17_골든패스-시나리오.md), [14](14_역할-분담.md), [10](10_전체-흐름-초안.md): "MCP 서버 3개" 표현 → "앱 1개 + 레지스트리 모듈 3개"
- 역할별 가이드: 레지스트리 기준으로 작성
