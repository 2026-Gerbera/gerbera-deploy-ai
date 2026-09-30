# 하네스 안내 (💭 v2, 2026-09-30)

> 기준: 앱 1개 구조(✅ 9/30 최신 개발자 문서 기준)
>
> 역할 코드: O1 정준우 · O2 김준석 · O3 장민영 · C1 유상준 · C2 안승환 · C3 양서윤 · TL = 하네스·계약 승인자(미지정, 결정 필요).

6명이 3일 동안 병렬로 개발할 때 전원이 같이 쓰는 저장소 구조, 도구, 규칙, 강제 장치입니다. v1(MCP 서버 3개 전제)을 앱 1개 + 툴 레지스트리 구조로 옮긴 판입니다. 팀이 앱 1개 구조에 반대하면, 레지스트리에 입력·출력 스키마가 있으므로 레지스트리를 MCP 서버로 노출하는 얇은 어댑터(P2)를 붙이는 대안이 있습니다.

- 표기: **✅ 확정** / **🟡 팀 합의 대기** / **💭 고려안**(추천일 뿐, 하네스 기본값으로 씀) / **⏸ 보류**. **[I-n]**은 [06 결정 필요 항목](06_결정-필요-항목.md) 번호입니다. "장부 n"은 2026-09-30 결정 장부 항목입니다.
- 하네스는 데이터 스키마의 필드(plan.json, 이벤트, deploy.yaml 키, 툴 입출력 필드)를 확정하지 않습니다. 실행기 최소 예시에 필요한 초안만 두고 `TODO(contract)`로 표시합니다. 확정은 [공통 계약 문서](../contracts/README.md)가 합니다.
- 설계 문서는 `single-app/docs/`, 최신 개발자 문서는 `single-app/dev-docs/`, 조사 근거는 `research/`에 보존합니다.

## 문서

| 문서 | 내용 |
|---|---|
| [00 병렬개발 준수사항](00_병렬개발-준수사항.md) | 반드시 지킬 것 18개, 알고 있어야 할 위험 21개. 확인 방법과 담당 |
| [01 저장소 구조와 소유권](01_저장소-구조와-소유권.md) | 트리(💭 레지스트리 트리 9/30), 구조 원칙, 40개 툴의 모듈 배치·step 층·담당, CODEOWNERS 규칙, **명령 표준(make 타깃 표)** |
| [02 툴 작성 규약](02_툴-작성-규약.md) | 툴 디렉토리, 입출력 모델, 레지스트리(`@tool`), 실패 보고, 실행 규칙, AI 경계 네 겹(한 프로세스판), call_ai backend, 신뢰하지 않는 입력, 완료 기준 |
| [03 깃 규칙과 AI 컨트리뷰터 차단](03_깃-규칙과-AI-컨트리뷰터-차단.md) | 브랜치·커밋·PR, 계약 변경 절차, 통합 창구, AI 트레일러·작성자 차단 층, 첫날 확인, 제출 전 확인, 비상 절차 |
| [04 AI 코딩 도구 사용 규칙](04_AI-코딩-도구-사용-규칙.md) | AGENTS.md 해설, Claude Code·Codex 설정, 한계, AI 활용 기록 |
| [05 devpi-guardian 참고](05_devpi-guardian-참고.md) | 하네스 사용 여부 판정, 라이선스, 가져올 것·바꿀 것·버릴 것 |
| [06 결정 필요 항목](06_결정-필요-항목.md) | I-1~I-35 상태(장부로 결정된 것 표시)와 결정 기록 방법 |

## 하네스 파일 지도

| 파일 | 역할 | 소유 |
|---|---|---|
| `AGENTS.md`, `CLAUDE.md` | AI·사람 공통 규칙, Claude Code 전용 주의 | 하네스 소유자(@TL, 지정 대기 [I-30]) |
| `CONTRIBUTING.md` | 사람용 온보딩 | 하네스 소유자 |
| `pyproject.toml` | 패키지 `ddak`(src 레이아웃), ruff·pyright·pytest·import-linter 설정 | 하네스 소유자 |
| `Makefile`, `scripts/dev.py` | 명령 단일 진입점(Makefile은 얇은 래퍼) | 하네스 소유자 |
| `scripts/git_guard.py` | AI 트레일러 제거와 AI 작성자 표시 검사(훅·CI 공용) | 하네스 소유자 |
| `.githooks/{commit-msg,pre-commit,pre-push}` | git_guard를 부르는 bash 래퍼 | 하네스 소유자 |
| `.claude/settings.json`, `.claude/hooks/guard_bash.py` | attribution 끄기, 비밀 읽기 차단, 💭 Bash 가드 훅 | 하네스 소유자 |
| `.github/workflows/ci.yml` | 필수 체크 3개(quality, attribution, secrets) | 하네스 소유자 |
| `.github/CODEOWNERS`, PR·이슈 템플릿 | 리뷰 배정(자리표시자 옆에 실명 주석), PR 검증 증거 칸 | 하네스 소유자 |
| `src/ddak/core/registry.py` | 툴 40개 카탈로그(메타정보)와 `@tool` 등록 규칙의 유일한 출처 | 하네스 소유자(계약) |
| `src/ddak/core/ai/` | `call_ai`·`ask_jev` 관문, provider(cli/api/replay/jev), LLM 연결 상태 | O2 김준석 |
| `src/ddak/executor/engine.py` | 실행기 최소 예시(트랙 병렬 + wait_for/signal + 트랙별 롤백 + `run: finally` 보고) | O1 정준우 |
| `src/ddak/app.py` | 조립 진입점(툴 모듈 자동 등록, 관리 웹 생성) | 하네스 소유자 |
| `src/ddak/core/tools/ping/` | 툴 템플릿(복사해서 시작) | 하네스 소유자 |
| `src/ddak/cd/interface.py`, `src/ddak/cd/providers/` | CD 공통 인터페이스 + provider(aws·onprem·fake) 골격(✅ 9/30 구조, 구현 TODO) | 하네스 소유자 + O1 정준우 / C2 안승환 |
| `src/ddak/ci/registries/` | 이미지 저장소 어댑터(기본 Docker Hub, ECR 옵션, digest 고정 참조) | C2 안승환 |
| `src/ddak/infra/providers/aws.py` | AWS 코드 소유 틀 자리(💭 state 층 분리 등) | C1 유상준 |
| `scripts/export_schemas.py` | 계약 스키마·툴 카탈로그 스냅샷 | 하네스 소유자 |
| `scripts/contract_smoke.py` | 레지스트리 구현 현황(등록/미구현). Fake 호출은 TODO(O1) | O1 정준우 |
| `scripts/patch_eval.py` | 골격(지원 설정 패턴은 P0·데모 ON, 일반 실행 기본 OFF) | O3 장민영 |
| `compose/local/compose.yaml` | 온프렘 db tier(MySQL 8.4.11) + 네트워크 템플릿 | O2 김준석 |
| `fixtures/plans/golden_v2_update.json` | 골든 패스 v2 plan.json 예시(설계 문서 02 6-4와 같음, 데모 시크릿 장면의 인프라 step 포함) | O2 김준석 + O1 정준우 |
| `tests/contract/` | 카탈로그·등록 규칙·AI 경계·backend·스키마·골든 plan.json·CI 약화 방지·git_guard·Bash 가드 테스트 | 하네스 소유자 |
| `tests/unit/` | 실행기·ping·관리 웹·redact·CD 인터페이스·이미지 저장소 어댑터 테스트 | 각 담당 |

## 최소 도입 순서

2026-09-30 사용자 결정: 3일 개발 일정에 맞춰 협업 절차를 최소화합니다. [최신 Git·CI 기준](03_깃-규칙과-AI-컨트리뷰터-차단.md)이 이전 문서의 이메일·커밋 형식·main/PR 승인 규칙보다 우선합니다.

1. `harness/`에서 작업합니다. CI는 저장소 루트의 `.github/workflows/ci.yml`이 이 폴더를 검사합니다.
2. `make setup-local` → `make ci`로 개발을 시작합니다. Git 훅 설치는 `make setup`입니다.
3. main push 또는 PR을 사용합니다. 이메일 등록·제목 형식·브랜치명·필수 승인·CODEOWNERS 설정은 요구하지 않습니다.
4. main push와 PR 생성·수정에서 CI 세 잡이 자동 실행됩니다. 실패 원인을 확인하고 수정합니다.

현재 CI는 개발 코드 검사이며 제품의 배포 기능은 별도로 구현합니다. 최신 O1 계약은 [착수 결정](../decisions/2026-09-30-o1-start-contracts.md)을 따릅니다.

## v1(MCP 3개) → v2(앱 1개)에서 바뀐 것

| v1 | v2 | 이유 |
|---|---|---|
| `packages/{plan,pipeline,report}_mcp` 서버 3개, stdio 자식 프로세스 | `src/ddak/{plan,infra,ci,cd,verify}` 모듈(💭 레지스트리 트리 9/30, 옛 `plan_validate`/`build_deploy`/`verify_report`를 대체), 한 프로세스 | 🟡 장부 3. LLM이 툴을 부르지 않으므로 MCP의 재사용 가치를 쓰지 않음 |
| `mcp_kit.ToolApp` + `@app.tool(NAME)` + `payload` 인자 | `registry.@tool(NAME)` + `(inp, ctx)` 시그니처 | 레지스트리 직접 호출(✅ 장부 6) |
| 실행기 `ToolGateway`(MCP Client 3개) | `executor.engine.Executor`가 `REGISTRY.get(name).fn` 직접 호출 | 같은 이유 |
| 서버별 env 허용목록(물리 겹) | 한 프로세스라 **없음**. 💭 컨테이너 모드의 웹/실행기 분리로 대체. CLI subprocess env 허용목록은 유지 | 정직한 한계(02 C-6) |
| `tools_list.<server>.json` 스냅샷, `make inspect` | `tool_catalog.json` 스냅샷. inspect 없음 | MCP 없음 |
| import-linter 5개(서버 기준) | 7개(모듈 기준, AI 툴 9개만 AI 관문 import, 검사기(계획·Dockerfile·인프라)는 생성기와 분리) | 모듈 경계 |
| AI provider 미정 | `call_ai` backend cli/api/replay + Jev 자리 | ✅ 장부 7·8 |
| Postgres 가정 | MySQL(compose db tier, env.example) | ✅ 장부 12 |
| 레지스트리 `ToolSpec`(dataclass) | pydantic `ToolSpec` + step 층·영향·타임아웃·canonical | ✅ 장부 3·5 |

## 하네스가 명세와 다르게 구현한 부분

| 명세 | 구현 | 이유 |
|---|---|---|
| `anyio_backend` 픽스처는 `tests/conftest.py` | 저장소 루트 `conftest.py` | 모든 테스트에 적용되게 |
| 툴 등록은 중앙 목록 | `ddak.app`이 `tools/` 아래를 이름순 자동 탐색(`import_tools`) | 같은 모듈의 담당자 여럿이 한 목록을 고치는 충돌을 없앰 |
| 레지스트리 메타정보는 데코레이터 인자 | 메타정보는 카탈로그(`registry.py`)에, 데코레이터는 이름만 | 40개 스냅샷을 구현 전부터 고정하고, 실행기·계획 검증이 툴 모듈을 import하지 않게 |
| import-linter protected 허용 목록은 툴 패키지 이름 | 와일드카드 `…<툴>.**`(패키지 자신은 제외) | 디렉토리가 생기기 전에는 이름을 쓰면 lint-imports가 "not present in the graph"로 실패(실측) |
| pre-commit은 `uv run ruff` | `.venv`의 ruff만 사용, 없으면 `make setup` 안내 | 커밋 중에 의존성 동기화가 일어나지 않게 |
| commit-msg는 AI 트레일러 제거 | 여기에 더해 author/committer가 AI 신원이면 거부 | `--author`, `GIT_AUTHOR_*` 경로를 커밋 시점에 조기 차단 |
| 테스트 가짜 비밀값 | 문자열을 이어 붙여 만듦 | gitleaks 예외 설정 없이 CI secrets 잡을 통과 |
| 읽기 전용 AWS 프로필 | 값이 없으면 `ddak-readonly`로 고정(`cd/providers/aws.py`·`ci` 규칙) | 기본 프로필(쓰기 권한)로 떨어지지 않게(fail closed) |
