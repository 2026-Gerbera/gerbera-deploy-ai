# O1 · PR #26 결정 12 통합 인계 — 2026-10-03

## 범위와 병합 상태

- 작업 위치: `.worktrees/patch-d12`, 브랜치 `o1/patch-d12`.
- 기준: `origin/o1/int-1112` / HEAD `3778537519120c8cd86222f9df769c05339792c7`.
- 수용: [민영님 PR #26](https://github.com/2026-Gerbera/gerbera-deploy-ai/pull/26), `origin/o3/roles-doc` `373a8efc2918e8dde0682f2cae6fb3f3623c8594`.
- `git merge --no-commit --no-ff origin/o3/roles-doc`의 충돌 6개를 해결하고 작업 파일 33개를 stage했다. 미해결 index와 충돌 표식은 0개다. HEAD는 기준 커밋 그대로이며 MERGE_HEAD는 위 O3 SHA로 남아 있다. 병합 커밋은 정준우가 한다. `.venv`는 저장소 루트 `.venv`를 가리키는 `../../.venv` 링크이며 stage 대상이 아니다.
- 결정 12의 내용 판정은 툴, 승인·원장 무결성과 이월 검사는 실행기에 둔다. 실제 AWS·VM·Docker·Claude·인프라 적용은 실행하지 않았다. 회귀/E2E는 fixture를 사용하고 Git E2E는 임시 로컬 저장소만 사용한다.

## 충돌 해결과 역할 분리

| 파일 | 통합 기준 |
|---|---|
| `src/ddak/plan/patch/generate.py` | int-1112 등록 툴·session·intents 경로 유지. PR #26 파일별 재적용·손실 우선 처리, 실제/호환 프롬프트 v3 통합 |
| `src/ddak/plan/patch/__init__.py` | 제품 경로의 공개 이름과 기존 호환 API 유지 |
| `src/ddak/plan/patch/README.md` | 제품/호환 API, OFF 재사용과 손실 중단, 필수 읽기 규칙 명시 |
| `src/ddak/core/contracts/tools/patch_config.py` | `status=patch_lost` 추가형. 실패 diff·값을 반환하지 않고 코드·파일·줄만 반환 |
| `harness/contracts/schemas/patch_config.output.json` | `contracts-update`로 재생성 |
| `harness/tests/unit/plan/patch/test_patch_config_tool.py` | int-1112 registry·passed/hash 회귀와 PR #26 손실 회귀를 함께 유지 |

`plan/patch/history.py`가 이전 diff 복원과 손실 판정의 공통 구현이다. 제품 `pipeline.prepare_patch`와 호환 `propose_config_patch`가 같은 함수를 사용한다. 파손된 diff는 빈 이력으로 취급하지 않는다. CRLF와 마지막 개행 없는 파일도 바이트를 유지한다.

`pipeline.prepare_patch`는 승인 패치를 파일별로 현재 `check_patch` 규칙에 대조한다. 정상 파일은 재사용한다. 옛 `env_optional` 패치는 ON에서 해당 파일만 재제안하고 OFF에서는 손실로 중단한다. AI 실패·빈 응답·형식 오류가 나도 손실 판정은 생략하지 않는다. 기존에 있던 선택적 읽기는 다른 설정을 수정했다는 이유만으로 거부하지 않고, 새로 생기거나 바뀐 선택적 읽기를 AST와 설정 대상(할당·keyword·dict 키), 제어 스코프와 같은 항목의 출현 순서로 검사한다. 다른 설정으로 옮긴 선택적 읽기도 새 읽기로 거부한다.

`core/patch_ledger.py`는 저장 diff·원본/결과 해시·환경 간 동일 파일 이력의 일치를 검증한다. 옛 semantic owner/표현식 고정 판정은 없앴다. 원장 필드는 호환을 위해 유지하지만 손실 판정에 쓰지 않는다. 개발자가 값 줄이나 파일을 제거하면 툴의 손실 판정은 통과한다. 미빌드 이미지의 승인 트리와 새 트리가 다르면 이월 검사에서 별도로 거부한다.

`app._prepare_config_patch` → registry `patch_config` → `PatchPreparation.from_output`은 `patch_lost`를 승인 전에 `PRECONDITION_FAILED`로 중단한다. 결과 화면은 `패치 툴 손실(patch_lost): 파일:현재 줄 (값 가림)`을 표시한다. `PlanBundle.patch_review`를 실행기로 전달하며, 실행기는 소스 내용을 재판정하지 않고 툴 결과와 승인할 실제 diff를 대조한다. 이전 원장이 있으면 패치 유무와 무관하게 툴 판정 누락을 거부한다. `passed is True`·실제 diff SHA-256·승인 트리·이월 해시 검사는 유지한다.

## 민영님께 공유할 변경 목록

1. PR #26은 정준우가 int-1112 위에서 통합한다. 민영님 브랜치 재정렬은 필요 없다.
2. 실제 제품 진입점은 int-1112의 registry/session/intents 경로다. `ctx.previous_release`의 환경별 성공 원장을 사용하며 `inp.previous` 호환도 유지한다.
3. 손실 규칙은 `plan/patch/history.py` 한 곳을 수정한다. 원장에는 같은 내용 판정을 추가하지 않는다. 파일별 재사용 적합성은 `pipeline.prepare_patch`, 형태·필수 환경 읽기는 `check_patch`다.
4. `patch_lost` 결과는 `passed=False`, patch/meta 없음, violations의 파일·현재 줄만 제공한다. O1이 승인 전 중단·화면 표시를 연결했다.
5. 제품 프롬프트 `patch_config-intents-v3`, 호환 프롬프트 `patch_config-v3`를 사용한다. 실패 응답의 raw diff·AI 이유는 승인 대상으로 노출하지 않는다.
6. `env_optional` 검사에서 이미 있던 읽기를 보존하는 오탐을 보완했다. 한 파일이 무효여도 다른 정상 재사용분은 유지한다.
7. O3 역할 문서, S0.ready의 클라우드 헬스 기준 정렬, 결과 보관소 예외 표시는 PR #26 내용으로 받았다. int-1112에 이미 있던 공통 smoke/domain 코드는 유지한다.
8. 등록 툴 출력 계약과 생성 스키마, 단위·E2E, 결정 12 기록, O1 기록을 함께 변경했다. 새 설정 키·툴·step·이벤트는 없다. `patch_review`는 내부 승인 연결 데이터다.

## 검증

**DONE.** 독립 정적 검토 PASS, 최종 전체 CI exit 0, 작업 파일 33개 stage 완료. 최종 증거는 `ci-complete.log`와 입력 해시를 기록한 `ci-complete-summary.json`이다. 로그는 `harness/var/validation/patch-d12/`에 있다. 검증 증거는 원래 파일명 뒤 `.txt`를 붙였고 `test_*.py` 사본은 0개다.

- **최종 전체 CI: 3573 passed / 0 failed / 1 skipped / 4 deselected.** lint/type/boundary/contracts/test 모두 PASS. boundary 테스트 51 passed, import 계약 7 kept / 0 broken. 2026-10-03 09:00:28 UTC(18:00:28 KST) 완료, 전체 270.168초. 소스·테스트·스키마 등 입력 548개 전후 해시 동일, 변경 0개. 소켓 제한 실패도 없다. skip 1개는 별도 temp-box uv 프로젝트에서 실행하도록 지정된 샘플 앱 테스트다.
- 명령: `UV_NO_SYNC=1 PYTHONPATH=<patch-d12>/src PYTHONDONTWRITEBYTECODE=1 make -C harness ci`.
- 스키마: 같은 환경으로 `make -C harness contracts-update` 실행, 55개 스키마 생성. 실제 변경은 patch_config 출력 스키마다.
- 중간 회귀: 1차 417 passed / 9 failed. 기존 optional 읽기 오탐, 이전 문구 기대값, UI 테스트의 httpx2 import를 수정했다. 2차 558 passed / 2 failed. 잘린 diff EOF 검증과 다중 줄 optional 읽기 진단을 수정했다.
- 최초 전체 CI: 3561 passed / 0 failed / 1 skipped / 4 deselected, boundary 51 passed, lint/type/boundary/contracts PASS, 229.1초. 이후 최종 정책 검사 위치와 독립 검토 지적을 보완했으므로 최종 근거는 후속 검증이다.
- 독립 검토 보완: 이전 원장이 있는 직접 prepare에서 툴 판정 생략을 거부하고, optional 읽기를 다른 설정으로 옮기는 경우도 신규 읽기로 차단했다. 관련 회귀 548개 통과(`review-regression-verified.log`), 마지막 중복 dict 키 보완은 134개 통과(`dict-scope-regression.log`). 독립 정적 재검토 최종 PASS이며 보고된 지적은 모두 해소했다(`review-summary.md`). 최초 BLOCKED/중간 REVISED는 수정 전 판정이다. 외부 Claude는 호출하지 않았다.
- 증거 구분: `ci-verified.log`와 `ci-verified-final.log`는 pytest가 통과했어도 검토 보완 중 입력 파일 2개가 바뀌어 최종 증거로 쓰지 않는다. 수정 후 고정된 입력으로 실행한 `ci-complete.log`만 최종 CI 근거로 삼는다.
- Git 이력 비밀 검사: `gitleaks git --redact --no-banner --no-color --log-opts='HEAD MERGE_HEAD -- . <금지 경로 제외>'` — 207 commits, 발견 없음, exit 0. 사용자 금지 경로 `.env`, `.env.*`, `.secrets/`, tfstate, `.aws/`, `.ssh/`, `.claude/`는 pathspec으로 제외했다. 결과 `gitleaks-history.log`, 가린 JSON 보고서 `gitleaks-history.json`. 변경 33개 파일의 `.txt` 증거 사본도 `gitleaks dir`로 검사해 발견 없음(`gitleaks-changes.log/json`).

## 결정 기록

[결정 12 구현 위치와 역할 분리](../decisions/2026-10-03-decisions-catch-up.md#103-pr-26-통합-구현-위치와-역할-분리)와 [O1 기록](../ai-usage/O1.md)에 반영했다.
