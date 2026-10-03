# gerbera-on-premise — 딸깍 배포 파이프라인

> 기준: 앱 1개 구조(✅ 9/30 최신 개발자 문서 기준)

> 로컬에서 만든 앱을 딸깍 한 번으로 로컬(온프레미스)과 클라우드에 동시에 배포하면서, 환경 차이로 깨지는 부분은 AI가 고치고, 두 환경이 똑같이 동작하는지 교차 검증까지 끝내는 파이프라인입니다.

SoftBank Hackathon 2026 Term1 팀 저장소입니다. 저장소 이름은 `gerbera-on-premise`, 파이썬 패키지 이름은 `ddak`입니다.

> **현재 실행 위치는 저장소의 `harness/`입니다.** 구형 MCP 하네스는 삭제했습니다. 개발 기준은 [최신 개발자 문서](../single-app/dev-docs/README.md)와 [하네스 안내](docs/harness/README.md)입니다. 최신 결정은 [10/3 결정 따라잡기](docs/decisions/2026-10-03-decisions-catch-up.md)입니다.

## 어떻게 동작하나

```
 파이썬 앱 1개 (src/ddak, 한 프로세스, 호스트에서 uv run, 127.0.0.1)   ✅ 구조(9/30), 10/1 cloud·onprem 분리 반영
 ├── web       관리 화면·채팅·계획 카드·승인 화면·2열 진행(SSE)·설정(도메인)·LLM 연결 카드
 ├── plan      ① 플랜(AI: 분석 분류·step 선택·Dockerfile 초안) + ② 계획 검증·Dockerfile 검사(코드)
 ├── executor  plan.json 순서대로 레지스트리 함수를 직접 호출(AI 없음)
 │             빌드 ∥ 로컬 트랙 ∥ 클라우드 트랙(환경 간 대기 없음) → 둘 다 성공 시 교차 검증
 ├── cd        ④ 배포 공통: interface.py(공통 인터페이스) + dispatch.py(provider 선택) + fake.py (AI 없음)
 ├── cloud     infra(AI Terraform: 생성만 AI, 검사·plan·사람 승인 뒤 apply는 코드) · build(③ 빌드·푸시, dockerhub 기본·ecr 옵션) · deploy(AWS provider) · tls · health
 ├── onprem    VM 기반: deploy(was VM 원격 Docker) · provision · inventory (AI 없음)
 ├── verify    ⑤ 검증 및 보고 (AI: 실패 원인 설명·보고 요약)
 ├── ops       운영(preflight, 데모 리셋, 정리)
 └── core      계약(pydantic) · 툴 레지스트리(@tool) · call_ai(cli/api/replay) · redact
                              │
   온프렘(VM 기반 web·was·db, 파이프라인은 was VM만 조작, 컨테이너 모드는 로컬 검증용, Docker Hub 읽기 전용 pull)
   · 온프렘 빌드는 실행 PC의 로컬 빌드 백엔드(CodeBuild와 같은 buildspec) → Docker Hub(10/3)
   · AWS 서울(CodeBuild → Docker Hub, ECS Fargate, 공유 RDS MySQL)
```

- 파이프라인: ① 플랜 → ② 계획 검증 → ③ 빌드 → ④ 배포 → ⑤ 검증 및 보고. "동시에" = 두 환경을 동시에 시작해 병렬로 진행합니다. 온프렘과 클라우드 사이에 대기 지점은 없고(10/1 밤), 한 환경이 실패해도 다른 환경은 계속합니다. 교차 검증은 둘 다 성공했을 때만 돕니다.
- AI는 제안만 만듭니다: JSON(채팅 의도, 분석 분류, step 선택, 실패 원인 설명, 보고 요약)과 Terraform HCL·IAM 정책 초안, (Dockerfile이 없을 때) Dockerfile 초안. 제안은 검증과 사람 승인을 거친 뒤 코드가 실행합니다(IAM 생성은 사람 승인 필수). 빌드·배포·검증 실행·롤백·잠금은 코드입니다. LLM은 툴을 직접 호출하지 않습니다. AI는 비밀값을 보지 않습니다.
- 이 경계는 CI가 검사합니다(import-linter 계약 7개 + `call_ai` 런타임 가드, [docs/harness/02](docs/harness/02_툴-작성-규약.md) C-6). 인프라 쪽 게이트(정적 게이트·정책 검사·Access Analyzer·사람 승인·권한 경계)는 [infra/terraform/README.md](infra/terraform/README.md).

## 빠른 시작

필요한 것: [uv](https://docs.astral.sh/uv/) 0.12.20 이상 0.13 미만, Python 3.13, make. 설치된 uv가 여러 개면 `command -v uv`와 `uv --version`을 확인합니다.

`harness/`에서:

```sh
make setup-local # lock 기준 의존성 설치. Git·계정 설정 변경 없음
make ci          # lint·type·경계·스키마·테스트. Git 작성자 검사는 별도
make run-fake    # 127.0.0.1:8765 관리 API 골격 + O1 실행 서비스
make demo        # 승인 후 fixture 전체 흐름. AWS·AI 실호출은 없음
make demo-local  # 실제 Docker 테스트 앱 배포/복구 (이미지 다운로드·빌드)
```

저장소 루트의 `.github/workflows/ci.yml`이 main push와 PR 생성·수정 때 `harness/`를 검사합니다. 일반 브랜치 push만으로는 실행되지 않습니다. `make setup`은 로컬 훅 설치용입니다. 이메일 등록·커밋 형식·필수 PR 승인은 없습니다. main에는 직접 push하지 않고 브랜치 + PR로 올리며 merge는 사람이 합니다(10/1). CI는 코드 검사이며 실제 배포는 하지 않습니다.

현재 계약은 [O1 착수 결정](docs/decisions/2026-09-30-o1-start-contracts.md)과 [계약 인덱스](docs/contracts/README.md)에서 시작합니다. 관리 웹은 FastAPI로 확정했고(✅ 9/30) 실행 코어는 웹과 분리했습니다. 팀원 연결 방법은 [O1 가이드](docs/guides/O1.md)를 봅니다.

전체 명령은 `make help`로 봅니다. 기여 방법은 [CONTRIBUTING.md](CONTRIBUTING.md), AI 코딩 도구 규칙은 [AGENTS.md](AGENTS.md), 사용법 요약은 [하네스 안내](docs/harness/README.md)에 있습니다.

## 현행 로컬 검증 (2026-09-30)

O1의 승인·SQLite 잠금/상태·패치 스냅샷·실행기·온프렘 provider를 구현했습니다. 실제 Docker 테스트 앱의 HTTP v1→v2→v1, 시크릿 재사용, 마이그레이션 러너 멱등성, 서비스 경유 반복 업데이트와 실패 복구를 검사합니다. 증거는 `var/validation/onprem-runtime.json`에 생성됩니다. 이 테스트는 실제 flaskr/MySQL·CodeBuild·AWS·AI 통합이나 3분 전체 데모의 증거가 아닙니다.

`make ci`는 lint, 타입, import 경계 7개, 계약 스키마와 외부 서비스 없는 테스트를 실행합니다. 실제 Docker 검사는 `DDAK_TEST_DOCKER=1 make test-docker`로 별도 실행합니다. `make preflight`는 Docker와 구현 등록 상태를 확인하며 팀 기능이 아직 없으면 종료 코드 3을 반환합니다. `make clean`은 캐시만 지우고 실행 DB·잠금을 보존합니다.

## 디렉토리

| 경로 | 내용 |
|---|---|
| `src/ddak/core` | 계약 모델, 툴 레지스트리(카탈로그 40개 + `@tool`), 어댑터 규약, AI 관문(`call_ai`, backend cli/api/replay, Jev 자리), redact, 예시 툴 `ping` |
| `src/ddak/plan` | ① 플랜 + ② 계획 검증(`generate_dockerfile` AI, `validate_dockerfile`·`validate_plan` 코드) |
| `src/ddak/cloud` | AWS 쪽: `infra`(AI Terraform: `generate_infra` AI, `discover_existing`·`validate_infra`·`plan_infra`·`apply_infra` 코드, `providers/aws` 코드 소유 틀) · `build`(③ `build_image`·`push_image`, `registries/dockerhub` 기본·`ecr` 옵션) · `deploy`(AwsProvider, 위임 구조는 PR #1 merge 뒤) · `tls`(`ensure_tls`) · `health`(클라우드 헬스·`verify_tls`) |
| `src/ddak/onprem` | 온프렘(VM 기반, was VM만 조작): `deploy`(OnPremProvider) · `provision` · `inventory` |
| `src/ddak/cd` | ④ 배포 공통: `interface.py`(deploy·rollback·health_check·migrate_db·inject_config·ensure_tls) + `dispatch.py`(`select_provider`) + `fake.py` |
| `src/ddak/verify` | ⑤ 검증 및 보고 |
| `src/ddak/ops` | 운영 툴(계획 밖) |
| `src/ddak/executor` | 실행기: 트랙 병렬(환경 간 대기 없음, 트랙 안 wait_for/signal), 트랙별 롤백, 진행 이벤트 |
| `src/ddak/web` | 관리 웹(FastAPI, 💭 Jinja2 + HTMX, SSE) |
| `src/ddak/app.py` | 조립 진입점(툴 모듈 자동 등록, `python -m ddak`) |
| `apps/sample-app` | 샘플 앱(배포 대상, flaskr 기반, MySQL) |
| `compose/local` | 온프렘 컨테이너 모드(로컬 검증용) 템플릿(MySQL 8.4 db tier) |
| `infra/terraform` | AI Terraform의 코드 소유 틀·정책 검사 규칙·권한 경계 템플릿(리소스 HCL은 AI가 생성, 💭 배치) |
| `contracts/schemas` | 계약 스키마·툴 카탈로그 스냅샷(생성물) |
| `docs/` | 하네스 규칙·계약·결정·AI 활용 기록과 이전 기획 자료 |
| `../single-app/` | 최신 설계·개발자 문서 |
| `../research/` | 조사 근거와 대화 자료 |

## 개발 과정의 AI 사용

이 저장소는 개발 과정에서 AI 코딩 도구(Claude Code, Codex 등)를 사용했습니다.

- 사용 기록은 사람별로 [docs/ai-usage/](docs/ai-usage/README.md)에 남깁니다(도구, 범위, 사람이 검증한 부분). PR 본문은 `.github/pull_request_template.md` 형식(변경 내용·확인한 것)을 따르고, 템플릿에 "AI 사용" 칸이 없으므로 "변경 내용"에 AI 사용 기록 위치를 한 줄 적습니다.
- git 메타데이터(커밋 작성자, Co-authored-by 트레일러)에는 AI를 넣지 않습니다. 이것은 GitHub 컨트리뷰터 목록의 문제이며, AI 사용 사실을 숨기려는 것이 아닙니다.
- 모든 커밋은 팀원 본인의 git 계정으로 만듭니다. main에는 직접 push하지 않고 브랜치 + PR로 올리며, merge는 사람이 합니다. 필수 리뷰 승인 수는 정하지 않았습니다.
- 제품이 실행 중에 쓰는 AI(채팅 의도, 분석 분류, step 선택, 실패 원인 설명, 보고 요약)의 호출 비용은 이와 별개로 결과 화면에 표시합니다.

## 라이선스

결정 대기([docs/harness/06](docs/harness/06_결정-필요-항목.md) I-19). 외부 코드 고지는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 있습니다.
