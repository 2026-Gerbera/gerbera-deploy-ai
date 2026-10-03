# plan/intake (담당: 정준우/O1, 2026-10-03 O2 승계)

`receive_deploy_request`: GitHub URL + ref를 코드로 검증하고, run 전용 checkout을 만들어 커밋을 고정하고,
저장소 루트 `deploy.yaml`을 읽고, 소스를 해시한다. AI 없음(import-linter 계약). 받은 코드는 실행하지 않는다.
읽기만 한다(clone). 읽기 전용 토큰을 권장한다.

## 공개 API (`__init__.py`)
- `receive_deploy_request(inp, ctx) -> ReceiveDeployRequestOutput`
- `cleanup_stale_sources(root, max_age_s=3600, *, keep=(), now=None) -> list[str]` (keep = 진행 중 run_id)
- `FetchPolicy` (테스트·flow가 정책을 만들 때. `FetchPolicy.from_env()`)

테스트용 주입: `logic.receive(inp, ctx, policy=, fetcher=)`, `fetch.fetch_repo(..., runner=, now=, sleep=)`.

## 환경변수 (`core/config.py`는 건드리지 않는다)
| 이름 | 기본 | 뜻 |
|---|---|---|
| `DDAK_GIT_ALLOWED_HOSTS` | `github.com` | 허용 호스트(쉼표). https만 허용 |
| `DDAK_GIT_TIMEOUT_S` | 120 | git 명령·잠금 대기 제한 |
| `DDAK_GIT_MAX_MB` / `DDAK_GIT_MAX_FILES` | 200 / 20000 | 저장소 크기·파일 수 한도 |
| `DDAK_SOURCES_DIR` | `var/sources` | 아래에 `cache/<project>/…`(캐시), `runs/<project>/<run_id>/`(run 전용 사본) |
| `DDAK_GIT_CACHE_TTL_S` | 3600 | 캐시 보존(받은 시각 기준). 새 푸시는 TTL과 무관하게 즉시 덮어쓴다 |
| `DDAK_GITHUB_TOKEN` | 없음 | 비공개용 읽기 전용 토큰. argv·URL에 넣지 않고 환경 설정으로만 git에 전달 |

출력 `source_dir`은 `DDAK_SOURCES_DIR`(policy.root) 기준 상대 경로다(예: `runs/demo/run1`). 성공한 run 사본은 보존한다
(O1 `prepare`/`materialize`가 같은 디렉토리를 쓴다. 재fetch 금지). 접수 실패 시 run 사본·`.partial`은 모두 지운다.

## 실패 대처 요약 (테스트: `harness/tests/unit/plan/intake/test_intake.py`)
F1 URL 형식·호스트·자격증명·옵션 주입 git 전에 거부 · F2 ref 검사, `--` 뒤에 URL · F3 git 미설치 · F4 일시 오류 재시도(1s,2s) ·
F5 타임아웃, 프로세스 그룹 종료 · F6 접근 불가/없음(재시도 없음, 토큰 안내) · F7 ref 없음 · F8 크기·파일 수 한도 ·
F9 서브모듈·LFS 거부 · F10 심볼릭 링크는 run 사본에서 삭제하고 계속(`ignored_symlinks`) · F11 deploy.yaml 없음·깨짐·모델 불일치 ·
F12 선언 경로가 없음 · F13 커밋 SHA를 한 번 해석해 고정 · F14 `.partial` 후 원자 rename, 실패 시 정리 · F15 run_id 중복 거부 ·
F16 접수 시점 해시 · F17 최소 환경(훅·전역 설정·프롬프트 차단) · F18 토큰·절대 경로 제거, stderr 200자 · F19 손상 캐시 폐기 후 재fetch ·
F20 캐시 키별 `flock` · F21 run은 캐시 복사본을 쓰므로 캐시 덮어쓰기와 무관.

실제 github.com 접속 테스트는 에이전트가 돌리지 않는다(사용자가 직접).

## 레포 감시 (`watch.py`, 플랜 10)
하드코딩 URL(`load_watch_targets`, `DDAK_WATCH_REPO_URL`로 덮어씀, 자리표시자면 시작 전 오류)의 `main`을 `DDAK_WATCH_INTERVAL_S`(기본 10초)마다
`git ls-remote`로 확인한다. 공개: `WatchTarget`, `Watcher(targets, on_new_commit, policy=, interval_s=, clock=, sleep=, resolve=, warm=)`,
`load_watch_targets`, `resolve_head`, `warm_cache`(fetch.py). 자동은 계획 생성·검증까지이며 배포 실행은 사람 승인 뒤다.
- 기준선: 상태 없음 → 현재 HEAD 기록 + 캐시 예열, 트리거 없음. 재시작 시 저장된 `last_commit`과 비교해 꺼져 있던 동안의 push도 감지.
- 합치기: 핸들러 실행 중 push는 최신 SHA 하나만 대기, 끝난 뒤 한 번 더. 핸들러 실패는 3회(간격) 재시도 후 처리 완료로 표시.
- 폴링 오류는 로그만, 지수 백오프(최대 60초). 상태 파일 `<DDAK_SOURCES_DIR>/watch/<project>.json`(원자적 쓰기, 토큰·경로 없음).
- T8(delta fetch 최적화)은 하지 않았다: 03의 F13·F14·F19~F21 안전성(원자 clone)을 건드릴 위험이 있고 `warm_cache`로 첫 clone 지연이 이미 줄어든다.
