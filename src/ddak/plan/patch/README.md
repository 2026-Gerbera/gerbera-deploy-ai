# plan/patch (담당: 장민영)

- 제품 진입점은 `tool.py`에 한 번 등록된 `patch_config`다. `app.py`는 레지스트리로 호출하며, `patch_session`으로 해당 run의 분석 결과·소스 루트·원장 루트·설정을 호출 동안만 주입한다. 설정의 비밀값은 툴 JSON에 넣지 않는다.
- 등록 툴은 `propose_intents` → 결정적 `render_intents` → `prepare_patch` 검사·원장 재사용으로 연결한다. 생성 입력은 위치와 허용 키 이름뿐이며 원본 코드·값·diff를 보내지 않는다. 이전 `propose_config_patch` API와 `PROMPT_VERSION=patch_config-v2`는 호환용으로 유지한다.
- 토글 ON은 새 제안을 허용한다. OFF여도 선택 환경의 성공 원장을 재사용하고, 새 원본에서 이전 승인 패치를 유지할 수 없으면 승인 전에 중단한다. 재사용 패치도 검사와 비밀값 스캔을 통과해야 한다.
- 출력의 `status`, `passed`, `patch`, `patch_sha256`, `meta`를 `PatchPreparation.from_output`이 검증해 승인 입력으로 바꾼다. `env_keys`, `env_vars`, `changed_files`, `warnings`도 계획으로 전달한다. 실행기 `prepare`는 토글과 무관하게 `passed is True`와 실제 UTF-8 패치 바이트의 SHA-256 일치를 요구한다.
- `env_vars`는 원본에 없던 필수 환경키만 담는다(최대 10개). `env_keys`는 재사용 패치와 변경 파일의 기존 필수 키도 포함하므로 둘의 크기는 다를 수 있다. 실제 신규 키가 계약 한도를 넘는 새 제안은 잘라 쓰지 않고 폐기한다.
- `patch_db_access`는 `patch_config`의 `local_address` 패턴으로 흡수했다. `DATABASE_URL`은 기본 허용 키에 포함하며 SQL은 바꾸지 않는다. `patch_db_access`·`patch_storage`는 별도 등록하지 않는다.
- 이미 환경변수를 읽는 위치는 패치 대상으로 선택하지 않는다. 대상이 없으면 `no_targets`, 새 제안이 실패하면 `rejected`와 경고를 반환하며 실패한 diff를 승인에 넘기지 않는다. 원장 재사용·손실 오류는 숨기지 않는다.
- 제품 의도 프롬프트는 `patch_config-intents-v2`다. 빈 `intents`는 재시도 없이 패치 없음으로 처리하고, 이전 성공 원장의 패치 손실 검사는 계속 적용한다.
- 호환 API `propose_config_patch`는 기존 줄 편집·재시도·빈 edits의 `no_targets` 동작을 유지한다. 제품 생성 경로에서는 호출하지 않는다.

- 검사(공통 계약 3-5): 형식(UTF-8, 64KB, 파일 5개 이하, 기존 텍스트 파일 수정만) → 허용 파일(정책 `allowed_files`, `.py`, tests·migrations 제외) → 허용 패턴(지운 줄은 대상 패턴만, 추가한 줄은 대상 패턴·환경변수 읽기·import·괄호/주석만, 위험한 호출과 `;` 거부, 대상 패턴을 지운 파일엔 환경변수 읽기 필요, 대상 패턴이 하나도 없으면 거부) → 비밀값 리터럴(비밀 이름이 있는 줄의 문자열은 키 자리만 허용: 환경변수 키, 첨자 키 `config["SECRET_KEY"]`, 딕셔너리 키 `{"SECRET_KEY": ...}`. 개발값 기본값 포함) → O1과 같은 `core.snapshots.apply_diff`로 임시 사본에 적용 → `ast` 문법 검사 → 원본과 AST 비교(새로 생긴 호출·import는 허용 목록만, 비밀 이름에 문자열을 넣는 곳이 늘면 거부)
- 형식(O1 `core.snapshots.apply_diff`와 같음): hunk 밖 줄은 `---`·`+++`·`@@`만(`diff --git`·`index`·모드 변경 등 Git 확장 헤더 거부), 파일마다 `---/+++` 쌍 하나와 바로 뒤 hunk 하나. 패치 생성 쪽은 `build_patch({경로: (원본, 수정본)})`로 이 형식을 만든다(파일 전체를 문맥으로 hunk 하나, 줄바꿈 유지)
- 우회 차단(PR #7 리뷰): 경로의 `..`·절대 경로·따옴표 거부, 추가한 줄의 패턴·환경변수 판정은 주석·문자열을 뺀 코드로, 비밀 이름이 있는 줄의 문자열은 키 자리만 허용(대문자 값도 거부)
- 대상 패턴(P0): 서명 키 하드코딩(`SECRET_KEY`), 코드 안 `localhost`·`127.0.0.1`, 쿠키 `SESSION_COOKIE_SECURE`, ProxyFix
- 위반은 코드·파일·줄 번호만 돌려준다(줄 내용을 싣지 않는다)
- 패치 생성 시 주의: 원본 줄바꿈(CRLF/LF)을 그대로 유지해야 적용된다(파일은 `newline=""`로 읽는다)
- 입출력 계약: `src/ddak/core/contracts/tools/patch_config.py`, 생성 스냅샷은 `harness/contracts/schemas/patch_config.*.json`이다.
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(여기는 허용: import-linter 계약 2). 검사기(`check.py`)는 AI를 import하지 않는다.
- 방어형 점검 반영(10/2, 새 AI 에이전트 리뷰): hunk 중간의 `\ No newline at end of file` 거부(git은 앞 줄 줄바꿈을 지워 다음 줄을 붙인다), 기존 일반 파일만 수정(`/dev/null` 없이 old 줄 수 0으로 만드는 새 파일 거부), `---`/`+++` 뒤 탭 접미사 거부(epoch 시각은 생성·삭제로 해석됨), 정규화되지 않은 경로(`//`, `./`) 거부, hunk 줄 수는 ASCII 7자리까지, 원본 파싱 실패·UTF-8 아닌 파일은 예외 대신 위반
