# devpi-guardian 하네스 검토 (2026-09-30)

> 대상: https://github.com/Beaver-Chain-Protect/devpi-guardian (클론 시점 main + 원격 브랜치 12개, 커밋 214개(merge 21개 포함), 개발 기간 2026-08-17 ~ 2026-08-26)
> 질문: ① 이 저장소를 개발할 때 하네스를 썼는가 ② 썼다면 우리 하네스로 가져올 내용이 있는가
> 방법: 저장소를 클론해 전체 이력(`git log --all`)과 파일을 직접 확인했습니다. 수치는 이 문서를 쓰며 다시 셌습니다.
> 판정 표기: **확인** / **일부 확인** / **미확인**
> 반영 위치: [../docs/16_병렬-개발-규칙과-하네스.md](../docs/16_병렬-개발-규칙과-하네스.md) 13절

## 1. 판정

**하네스를 써서 개발한 게 맞습니다(확인).** 다만 설정 파일이나 훅으로 만든 하네스가 아니라 **"문서 + CI"형**입니다.

| 구성요소 | 내용 | 근거 |
|---|---|---|
| AGENTS.md | 권위 문서 목록, 에이전트 역할(controller·reviewer = `gpt-5.6-sol`, 구현 = `gpt-5.6-luna` 서브에이전트), 9단계 필수 워크플로(worktree → 한 번에 한 태스크 → TDD → spec 리뷰 → 품질 리뷰 → controller 재검증), 엔지니어링 제약, 품질 명령 | `AGENTS.md` |
| 도입 시점 | 두 번째 커밋 `bb48b7a`(08-17 21:55). 설계+계획 커밋 `6d48991`(21:47) 직후, 첫 코드 커밋 `ffcd0d7`(21:59) 전 | `git log --reverse` |
| 외부 스킬 | 모든 plan 머리(3번째 줄)에 "REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development". 스킬 자체는 저장소에 없고 각자 설치에 의존 | `docs/superpowers/plans/*.md` |
| 설계·계획 문서 | `docs/superpowers/specs/` 7개, `docs/superpowers/plans/` 6개(날짜 붙은 파일) | 디렉토리 |
| 리뷰 기록 | `docs/reviews/` 2개. base/head SHA, 실행 명령, exit code, 테스트 개수, 미해결 finding 수를 적음 | `docs/reviews/2026-08-24-pr1-hardening-review.md` |
| CI + 변조 방지 | `.github/workflows/ci.yml` + `tests/test_package.py`가 ci.yml 전체 문자열 비교와 금지 문자열(`continue-on-error`, `contents: write`, `--ignore` 등) 검사 | `tests/test_package.py` |
| Codex 사용 흔적 | plan 규칙 "Use `apply_patch` for every file edit", 리뷰어 ID `/root/task6_independent_spec`·`/root/task6_independent_security`, 원격 브랜치 `codex/openssf-best-practices`(PR #11로 병합) | `docs/superpowers/plans/2026-08-25-pr5-pr9-safe-integration.md:19`, `docs/reviews/2026-08-24-pr1-hardening-review.md:9,12`, 커밋 `56168d0` |

**없는 것 (확인):** CLAUDE.md, `.claude/`, `.codex/`, git 훅, `.pre-commit-config.yaml`, Makefile·justfile, PR·이슈 템플릿, CODEOWNERS는 **전체 이력(모든 브랜치)에 한 번도 없습니다.** `git log --all --name-only`에서 하네스 성격의 파일은 `AGENTS.md`와 `.github/workflows/ci.yml`뿐입니다.

**팀 전체 적용 (일부 확인):** 규칙은 주로 한 사람(esc)의 에이전트 흐름에만 적용됐습니다. 다른 멤버의 커밋은 대부분 규칙 밖입니다(3절). 강제 장치(훅·CI 검사·CODEOWNERS)가 없었기 때문입니다.

**사용자 본인:** JungJoonWoo(정준우)도 F1/F2를 맡았습니다. 커밋 `2ab58c3`(08-20)에서 AGENTS.md 범위 규칙을 고치고 F1/F2 spec을 추가해 하네스를 따랐습니다.

## 2. 파일별 인벤토리

| 경로 | 역할 | 하네스 여부 | 우리 처리 |
|---|---|---|---|
| `AGENTS.md` (약 3.9KB) | 에이전트 운영 규칙 | **핵심** | 바꿔서 가져옴(4절 #1~#6) |
| `docs/superpowers/specs/` (7개) | 설계 spec. 머리 메타(상태·작성일·담당·범위), 범위·비범위, 공개 인터페이스, 다른 작업자 연결 방법, 완료 기준 | 문서형 하네스 | 템플릿 형식만(#10) |
| `docs/superpowers/plans/` (6개) | 실행 계획. Goal, Architecture, 파일 구조, Task별 Files·체크박스·Run/Expected·커밋 명령 | 문서형 하네스 | 이슈 템플릿 수준으로 축소(#11) |
| `docs/reviews/` (2개) | 독립 리뷰 기록 | 문서형 하네스 | PR 템플릿 "검증 증거" 칸으로(#3, #24) |
| `docs/analyzers/f8-f9-handoff.md` | F8/F9 인수인계 문서 | 부분 | 가이드 템플릿 참고 |
| `docs/f5-worker.rst`, `docs/f11-admin-api-cli.rst` | 기능 문서 | 아님 | – |
| `.github/workflows/ci.yml` | tests 잡(Python 3.11~3.14 매트릭스) + quality 잡(ruff, flake8, bandit, build) | 강제 장치 | 보안 설정만 가져옴(#7), 매트릭스 버림(#20) |
| `tests/test_package.py` | 패키지 계약 + CI 변조 방지 + README 문구 검사 | 강제 장치 | 금지 목록 방식만(#8), README 검사 버림(#22) |
| `pyproject.toml` | setuptools src 레이아웃, ruff(line 100, py311, E·F·I·UP·B·SIM·RUF), pytest(`-ra --strict-markers`, 마커 integration·performance), license MIT | 도구 설정 | 가져와서 수정(#9) |
| `.flake8` | line length만 설정 | 중복 도구 | 버림(#21) |
| `uv.lock` | 단일 lock | – | 같은 방식 |
| `.gitignore` | `.worktrees/` 포함 | 부분 | 가져옴(#5) |
| `CONTRIBUTING.md` | 수용 기준, 품질 명령 7개, "Pin GitHub Actions by full commit SHA" | 부분 | SHA 고정 규칙만(#15) |
| `README.md` (약 30KB) | 제품 문서 | 아님 | – |
| `SECURITY.md`, `CHANGELOG.md`, `news/*.feature` (10개) | 공개 SW 운영(취약점 신고, 변경 이력 조각) | 아님 | 버림(#22) |
| `LICENSE` | MIT | – | 5절 |
| `tools/` (`corpus_report`, `demo_scenario`, `fetch_pypi_corpus`, `integration_smoke`) | 제품용 분석기 평가·시연 도구. CI·AGENTS.md에서 부르지 않고 tests만 import | **에이전트 하네스 아님** | 패턴만(#14) |
| `src/devpi_guardian/` (65개 파일, 약 19.5k줄) | 제품 코드 | – | `privacy.py` 개념(#13), `worker/interfaces.py`의 Protocol 포트 패턴(#12) |
| `tests/` (약 32k줄) | 테스트 | – | – |

## 3. 개발 방식 증거 (git log)

### 3-1. 작성자 분포

| 작성자 | 커밋(merge 제외) | 커밋 규칙 준수 | 메모 |
|---|---|---|---|
| esc | 148 (merge 포함 150) | **148/148** | 에이전트 주도 흐름 |
| chaehj02 | 30 | 1/30 | 대부분 자유 형식 |
| setiyll | 7 | 1/7 | |
| ye11oc4t | 3 | 2/3 | `codex/openssf-best-practices` 브랜치 커밋 작성 |
| Pandyo | 3 | 3/3 | |
| JungJoonWoo | 1 (merge 포함 2) | 1/1 | F1/F2 |
| Eunso Choi | 1 | 0/1 | |
| **합계** | **193** (merge 21개 포함 214) | | |

- 준수 기준: 제목이 `<type>(<scope>)?: `로 시작(type = feat, fix, docs, chore, test, refactor, ci, build, perf, style, revert).
- committer는 사람 본인 또는 `GitHub <noreply@github.com>`(웹 merge 9건)뿐입니다.

### 3-2. esc 커밋 유형·날짜

| 유형 | fix | docs | feat | test | style | chore | perf | ci |
|---|---|---|---|---|---|---|---|---|
| 개수 | 73 | 35 | 23 | 11 | 2 | 2 | 1 | 1 |

| 날짜 | 08-17 | 08-18 | 08-21 | 08-24 | 08-25 |
|---|---|---|---|---|---|
| 커밋(merge 포함) | 7 | 47 | 9 | 4 | **83** |

- fix가 feat의 3배 이상입니다. 모든 태스크를 "spec 리뷰 → 품질 리뷰 → Critical/Important 0개까지 반복"한 결과로 보입니다(추정).
- 코드 약 19.5k줄에 테스트 약 32k줄입니다.

### 3-3. AI 컨트리뷰터 흔적

- 전체 214커밋 본문에서 `Co-authored-by`, `Generated with`, `Claude-Session`, `Signed-off-by`는 **0건**입니다(확인).
- 봇 작성자도 없습니다. Codex 작업도 사람의 로컬 git 계정으로 커밋됐습니다.
- **해석:** 도구 기본값(Codex CLI는 기본적으로 트레일러를 넣지 않음) 덕분이지, 강제된 결과가 아닙니다. Claude Code는 기본값으로 트레일러를 넣으므로 우리는 훅·CI로 강제해야 합니다([16](../docs/16_병렬-개발-규칙과-하네스.md) 7절).

### 3-4. 병렬 브랜치 통합 실패 (가장 중요한 교훈)

| merge 커밋 | 날짜 | 충돌 파일 |
|---|---|---|
| `4ef3b99` | 08-23 | `AGENTS.md`, `news/3.feature` |
| `aa43370` | 08-23 | analyzers 모듈 4개와 테스트 4개, `tools/demo_scenario.py` |
| `091dbfc` | 08-24 | `tests/test_package.py` |
| `4a21d3a` | 08-25 | `news/9.feature`, `src/devpi_guardian/plugin.py` |
| `16f343e` | 08-25 | `tests/admin/test_views.py`, `tests/test_package.py` |

- feature 브랜치들이 main(과 서로)을 반복해서 merge해 들이며 공유 파일에서 충돌했습니다.
- news 번호 충돌을 고치는 커밋이 따로 필요했습니다(`b821bb3`, 08-24).
- 결국 08-25 통합 설계는 "PR #5와 PR #9는 main에 직접 merge하지 않는다. 기능 단위로 선택 이식한다"로 정했습니다. migration 번호와 `plugin.py` 구조를 하나로 다시 설계해야 했습니다(`docs/superpowers/specs/2026-08-25-pr5-pr9-safe-integration-design.md` 1절).
- 병목은 중앙 조립 파일(`plugin.py`), 공유 테스트 파일(`tests/test_package.py`), 번호 붙은 파일(news 조각, SQL migration)이었습니다.

### 3-5. 문서·명령 드리프트

- **plan 드리프트:** `2026-08-17-f3-f4-enforcement-verdict-store.md`는 2956줄이고 코드 스니펫이 가득합니다. 구현 중 사실과 달라져 머리에 "이 correction이 아래 스니펫을 대체한다"는 배너가 7개 붙었습니다(모두 08-18).
- **권위 문서 목록 미갱신:** AGENTS.md의 권위 문서 목록에 08-18 allowed-release spec과 08-25 PR5/PR9 spec·plan이 없습니다.
- **품질 명령 불일치:** AGENTS.md(pytest, ruff, flake8, build), CONTRIBUTING(여기에 bandit, `uv lock --check` 추가), CI(`python -m build`, lock 확인 없음)가 서로 다릅니다.

## 4. 가져올 것·바꿀 것·버릴 것

| # | devpi 원본 | 처리 | 우리 버전 |
|---|---|---|---|
| 1 | AGENTS.md 골격(권위 문서, 우선순위, 불일치 시 중단) | 바꿔서 가져옴 | 권위 문서는 인덱스 링크 한 줄. "중단"은 "30분 안에 TL에게 NEEDS_CONTEXT". TL 소유, 전용 PR로만 수정 |
| 2 | 구현자는 설계 결정을 하지 않고 NEEDS_CONTEXT/BLOCKED로 보고 | 가져옴 | 모델명은 빼고, 사람 담당자가 자기 모듈의 controller |
| 3 | "에이전트 보고만 믿고 통과라 하지 말고 새로 검증" | 가져옴 | PR 템플릿의 검증 증거 칸 |
| 4 | Engineering constraints 목록 형식 | 바꿈 | AI 경계 불변 조건 (a)~(h) |
| 5 | `.worktrees/` 격리, main에서 구현 금지 | 가져옴 | worktree 하나에 에이전트 하나 |
| 6 | 사람 결정 게이트(force push 금지, Ready 전환은 사람) | 확장 | merge, 태그, `terraform apply`, AWS 삭제, 비밀 입력까지 |
| 7 | CI `contents: read`, concurrency, SHA 고정, `uv sync --locked` | 가져옴 | 같게 하되 Python 3.13 단일 |
| 8 | CI 약화 방지 테스트(ci.yml 전체 문자열 비교 + 금지 문자열) | 바꿈 | 금지 목록 방식만. 전체 비교는 6명이 CI를 고치기에 너무 경직돼 버림 |
| 9 | ruff(line 100, E·F·I·UP·B·SIM·RUF), pytest `-ra --strict-markers` | 가져옴 | py313, T20·TID·S 추가, `--import-mode=importlib`, 마커 docker/aws/llm |
| 10 | spec 템플릿(범위·비범위, 공개 인터페이스, 다른 작업자 연결 방법, 완료 기준) | 바꿈 | `docs/guides/_TEMPLATE.md`, `docs/contracts/README.md` |
| 11 | plan 형식(2956줄, 코드 스니펫 가득 → 정정 배너 7개) | 바꿈 | 이슈 템플릿 수준. plan에 구현 코드를 미리 쓰지 않음 |
| 12 | `typing.Protocol` 포트 + fake 주입 | 가져옴(패턴) | `TargetAdapter` Protocol, 툴별 `fake.py` |
| 13 | `privacy.py`(로그 정제, 4096자 제한) | 개념만 | `ddak_core/redact.py` 새로 작성, AWS 키·DB URL·.env 패턴 추가 |
| 14 | `tools/integration_smoke`·`demo_scenario`·`corpus_report` | 패턴만 | `contract_smoke`, `patch_eval`(타임스탬프 없는 결정적 JSON), `demo-reset` |
| 15 | CONTRIBUTING의 "Action 전체 SHA 고정" | 가져옴 | 준수사항 #14 |
| 16 | conventional commits(강제 없음) | 강제로 바꿈 | commit-msg 훅과 CI, scope 고정 |
| 17 | 모델명 고정 역할(gpt-5.6-sol/luna) | 버림 | – |
| 18 | "구현 에이전트 동시 실행 금지" | 반대로 바꿈 | 모듈별 병렬 허용, 같은 모듈·공유 파일 동시 작업만 금지 |
| 19 | 모든 태스크 엄격 TDD, 2단계 리뷰를 finding 0까지 반복 | 버림 | 테스트 필수 영역만(방식 자유). 보안 민감 PR만 추가 리뷰, 에이전트 리뷰 반복은 선택 |
| 20 | Python 3.11~3.14 매트릭스 | 버림 | 3.13 단일 |
| 21 | flake8 중복, bandit 별도 | 버림 | ruff 하나(S 규칙이 bandit 역할) |
| 22 | README 문구 검사 테스트, news/, CHANGELOG, OpenSSF, SECURITY 신고 절차 | 버림 | – |
| 23 | Superpowers를 필수 스킬로 강제 | 버림 | 선택. 하네스 문서는 외부 플러그인 없이 읽히게 |
| 24 | `docs/reviews` 별도 리뷰 문서 | 바꿈 | PR 템플릿 |
| 25 | (교훈) 품질 명령이 여러 곳에 따로 적혀 서로 다름 | 반영 | `scripts/dev.py` 단일 진입점 + `make` 타깃만 문서에 적음 |
| 26 | (교훈) 공유 파일·번호 파일 충돌로 PR #5/#9를 직접 merge하지 못함 | 반영 | 계약 선확정, 1툴 1디렉토리, 서버별 등록 파일, 번호 파일 금지, 짧은 브랜치와 squash 병합, 매일 main 기준 E2E |
| 27 | (교훈) 강제 장치 없는 문서 규칙은 팀 전체에 적용되지 않음 | 반영 | 훅, CI 필수 체크, CODEOWNERS, main 보호 |

## 5. 라이선스 조건

| 항목 | 내용 | 판정 |
|---|---|---|
| 라이선스 | MIT License, "Copyright (c) 2026 devpi-guardian contributors". `pyproject.toml`에도 `license = "MIT"` | 확인 |
| 조건 | 소프트웨어의 사본 또는 **상당 부분**에 저작권 고지와 허가 문구를 포함. 보증 없음 | 확인 |
| 구조·규칙 참고 | 문서 구조나 규칙을 참고해 우리 말로 다시 쓰는 것은 의무가 없음 | 확인(MIT 문언 기준) |
| 코드 복사 | `privacy.py` 정규식, CI 계약 테스트처럼 코드를 상당 부분 가져오면 파일 머리와 `THIRD_PARTY_NOTICES.md`에 MIT 고지 | 확인 |
| 대회 규정 | 해커톤 이전 작업물(코드) 재사용이 허용되는지 | **미확인** → 운영진 질문 [I-22] |

**결론:** 이번 하네스는 devpi-guardian의 코드를 복사하지 않고 **전부 새로 씁니다.** 가져오는 것은 규칙·문서 골격·패턴뿐입니다. 코드를 복사해야 할 일이 생기면 대회 규정을 먼저 확인하고 고지를 남깁니다.

## 6. 확인하지 못한 것

| 항목 | 이유 |
|---|---|
| Superpowers 스킬의 버전·내용 | 저장소 밖(각자 설치) |
| esc 외 멤버가 AI 코딩 도구를 썼는지 | 커밋에 흔적이 없고, 트레일러가 없다고 AI를 안 썼다는 뜻은 아님 |
| esc의 흐름이 전부 Codex였는지 | 흔적(apply_patch, `/root/...` 리뷰어 ID, codex 브랜치)은 Codex를 가리키지만 모든 커밋을 구분할 근거는 없음 |
| 대회 전 작업물 재사용 허용 여부 | 해커톤 규정 미확인 |
