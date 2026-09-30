# 05. devpi-guardian 참고

> 기준: 앱 1개 구조(✅ 9/30 최신 개발자 문서 기준). 이 문서의 판정은 구조와 무관하게 그대로입니다. 조사 원문: [devpi-guardian 하네스 검토](../../../research/2026-09-30_devpi-guardian-하네스-검토.md).

대상: <https://github.com/Beaver-Chain-Protect/devpi-guardian> (전체 이력, 모든 브랜치 확인)

## 판정: 하네스를 써서 개발한 것이 맞습니다

- 형태는 설정 파일이나 훅이 아니라 **"문서 + CI" 조합**입니다.
  - AGENTS.md: 두 번째 커밋 `bb48b7a`(2026-08-17 21:55)로 들어왔습니다. 설계+계획 커밋 `6d48991` 직후이자 첫 코드 커밋 `ffcd0d7` 전입니다. 권위 문서, 에이전트 역할(controller·reviewer와 구현 서브에이전트), 9단계 필수 워크플로(worktree, 한 번에 한 태스크, TDD, spec 리뷰 → 품질 리뷰, controller 재검증), 엔지니어링 제약, 품질 명령을 적었습니다.
  - 외부 Superpowers 스킬(저장소에 포함 안 됨): 모든 plan 머리에 subagent-driven-development를 필수 스킬로 적었습니다.
  - `docs/superpowers/specs`(7개)·`plans`(6개), `docs/reviews`(2개).
  - CI와 CI 변조 방지 테스트(`tests/test_package.py`).
- **없는 것:** CLAUDE.md, `.claude/`, `.codex/`, git 훅, pre-commit, Makefile, PR 템플릿, CODEOWNERS는 전체 이력(모든 브랜치)에 한 번도 없습니다.
- 주로 한 사람(esc, 214커밋 중 150개)이 Codex로 돌린 흐름입니다. 근거: plan의 "모든 파일 편집은 apply_patch" 규칙, 리뷰어 ID 형식, `codex/…` 원격 브랜치.
- 커밋 규칙(conventional commits)을 esc는 148/148 지켰지만 다른 멤버는 대부분 자유 형식이었습니다. **강제 장치가 없었기 때문입니다.**
- AI 컨트리뷰터 흔적은 214커밋 전체에서 0건입니다(공동 작성자 트레일러, 생성 표시 문구, 봇 작성자 없음). Codex 작업도 사람 git 계정으로 커밋됐습니다. **도구 기본값 덕분이었을 뿐 강제된 결과가 아닙니다.**
- 사용자 본인도 F1/F2를 맡았고, 커밋 `2ab58c3`에서 AGENTS.md 범위 규칙을 직접 고쳤습니다.
- **가져올 것은 설정 파일이 아니라 규칙과 문서 골격입니다.**

## 라이선스

- MIT (Copyright (c) 2026 devpi-guardian contributors).
- 구조와 규칙을 참고해 우리 말로 다시 쓰는 것은 의무가 없습니다. 코드나 테스트를 상당 부분 복사하면 저작권 고지와 허가 문구를 넣어야 합니다.
- **이 하네스는 코드를 복사하지 않고 전부 새로 썼습니다.** 해커톤 이전 작업물 재사용 규정이 확인되지 않았기 때문입니다([THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md)).

## 가져올 것 · 바꿀 것 · 버릴 것

| # | devpi 원본 | 처리 | 우리 버전 |
|---|---|---|---|
| 1 | AGENTS.md 골격(권위 문서, 우선순위, 불일치 시 중단) | 바꿔서 가져옴 | 권위 문서는 인덱스 링크 한 줄. "중단"은 "30분 안에 하네스 소유자에게 NEEDS_CONTEXT". 하네스 소유자 소유, 전용 PR로만 수정 |
| 2 | 구현자는 설계 결정을 하지 않고 NEEDS_CONTEXT/BLOCKED로 보고 | 가져옴 | 모델명은 빼고 사람 담당자를 controller로 |
| 3 | "에이전트 보고만 믿고 통과라 하지 말고 새로 검증" | 가져옴 | PR 템플릿의 검증 증거 칸 |
| 4 | Engineering constraints 목록 형식 | 바꿈 | AI 경계 불변 조건 (a)~(h) |
| 5 | `.worktrees/` 격리, main에서 구현 금지 | 가져옴 | worktree 하나에 에이전트 하나 |
| 6 | 사람 결정 게이트(force push 금지, Ready 전환은 사람) | 확장 | merge, 태그, terraform apply, AWS 삭제, 비밀 입력까지 |
| 7 | CI `contents: read`, concurrency, SHA 고정, `uv sync --locked` | 가져옴 | 같게 하되 Python 3.13 단일 |
| 8 | CI 약화 방지 테스트(ci.yml 전체 문자열 비교 + 금지 문자열) | 바꿈 | 금지 목록 방식만(`tests/contract/test_ci_guard.py`). 전체 비교는 버림 |
| 9 | ruff(line 100, E,F,I,UP,B,SIM,RUF), pytest `-ra --strict-markers` | 가져옴 | py313, T20·TID·S 추가, `--import-mode=importlib`, 마커 docker/aws/llm |
| 10 | spec 템플릿(범위·비범위, 공개 인터페이스, 다른 작업자 연결 방법, 완료 기준) | 바꿈 | [docs/guides/_TEMPLATE.md](../guides/_TEMPLATE.md), [docs/contracts/README.md](../contracts/README.md) |
| 11 | plan 형식(2956줄, 코드 스니펫 가득 → 정정 배너 6개) | 바꿈 | 이슈 템플릿 수준. plan에 구현 코드를 미리 쓰지 않음 |
| 12 | typing.Protocol 포트 + fake 주입 | 가져옴 | `TargetAdapter` Protocol, 툴별 `fake.py`, 테스트용 `Registry` 따로 만들기 |
| 13 | privacy.py(로그 정제) | 개념만 | `ddak/core/redact.py` 새로 작성, AWS 키·DB URL(MySQL 포함)·.env·ARN 계정 ID 패턴 추가 |
| 14 | tools/integration_smoke·demo_scenario·corpus_report | 패턴만 | `contract_smoke`(레지스트리 등록/미구현), `patch_eval`(결정적 JSON), `demo-reset` |
| 15 | CONTRIBUTING의 "Action 전체 SHA 고정" | 가져옴 | [00](00_병렬개발-준수사항.md) 준수 #14 |
| 16 | conventional commits(강제 없음) | 강제로 바꿈 | 훅과 CI, scope 고정 |
| 17 | 모델명 고정 역할 | 버림 | – |
| 18 | "구현 에이전트 동시 실행 금지" | 반대로 바꿈 | 모듈별 병렬 허용, 같은 모듈 동시 작업만 금지 |
| 19 | 모든 태스크 엄격 TDD, 2단계 리뷰를 finding 0이 될 때까지 반복(fix 73 : feat 23) | 버림 | 테스트 필수 영역만 두고 방식은 자유. 보안 민감 PR만 추가 리뷰(에이전트 리뷰 반복은 선택) |
| 20 | Python 3.11~3.14 매트릭스 | 버림 | 3.13 단일 |
| 21 | flake8 중복, bandit 별도 | 버림 | ruff 하나(S 규칙이 bandit 역할) |
| 22 | README 문구 검사 테스트, news/, CHANGELOG, OpenSSF, SECURITY 신고 절차 | 버림 | – |
| 23 | Superpowers를 필수 스킬로 강제 | 버림 | 선택 |
| 24 | docs/reviews 별도 리뷰 문서 | 바꿈 | PR 템플릿 |
| 25 | (교훈) 품질 명령이 4곳에 따로 적혀 서로 다름 | 반영 | `scripts/dev.py` 단일 진입점 |
| 26 | (교훈) 공유 파일·번호 파일 충돌로 PR #5/#9를 직접 merge하지 못함 | 반영 | 계약(카탈로그) 선확정, 1툴 1디렉토리 + 자동 등록(`ddak.app`), 번호 파일 금지, 짧은 브랜치와 squash 병합, 매일 main 기준 E2E |
| 27 | (교훈) 강제 장치 없는 문서 규칙은 팀 전체에 적용되지 않음 | 반영 | 훅, CI, CODEOWNERS, main 보호 |
