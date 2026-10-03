# diagnose_parity_gap은 설명 전용(판정 영향 없음)

- 날짜: 2026-10-03
- 상태: 결정
- 관련 항목: 장민영(O3) 질문 [O3.md 2026-10-03 01:19](../ai-usage/O3.md)(passed 의미·실행 시점·입력), PR #16 검토의 redact 빈틈
- 결정자: 정준우(O1)

## 맥락

실행기(`src/ddak/executor/engine.py`)는 `diagnose_parity_gap`을 `compare_env_results`와 같은 묶음으로 다뤘다.
그래서 diagnose가 등록되어 `passed=False`를 돌려주면 PARITY_FAILED(클라우드 롤백)가 될 수 있었다.
diagnose는 AI가 허용된 툴이다. 불변 조건 (a)에 따라 AI 출력이 실행·롤백·판정에 영향을 주면 안 된다.
민영님은 이 이유로 툴 등록을 보류했다.

## 선택지와 장단점

| 선택지 | 장점 | 단점 |
|---|---|---|
| A. 지금처럼 compare와 같은 판정 툴 | 코드 변경 없음 | AI 출력이 롤백을 일으킨다. 불변 조건 (a) 위반 |
| B. 설명 전용 내장 step(채택) | 판정은 compare·환경 검증 결과로만 정한다. 카탈로그 메타는 그대로 둔다 | 보고에 diagnose 출력을 넘기는 연결은 따로 정해야 한다 |

## 결정

1. **passed 의미**: 실행기는 diagnose 출력의 `passed`를 run 상태·롤백·관문 판정에 쓰지 않는다. 툴이 정상 반환하면 step 기록은 항상 `succeeded`이고, `passed`를 포함한 출력 전체는 기록(`verify.diagnose`)에 그대로 남는다. 결과 화면·보고 설명에만 쓴다. 출력 모델에 `passed`를 둘지와 그 뜻은 O3가 정한다(두지 않아도 된다).
2. **실행 시점**: 카탈로그 층 `builtin`(실행기 내장), 대상 없음은 그대로 둔다. 계획에는 넣지 않는다. 계획에 diagnose 툴이나 `verify.diagnose` step id가 있으면 실행 전에 PLAN_INVALID로 거부한다.
   - compare가 불합격(`check_failed`, PARITY_FAILED)이거나, local·cloud 트랙이 FAILED·ROLLED_BACK·ROLLBACK_FAILED일 때만 돈다.
   - run 상태 판정과 환경 롤백이 끝난 뒤, `post_report`(finally) 전에 한 번 돈다. 진단이 복구를 늦추지 않는다.
   - 성공 run과 빌드 실패만 있는 run에서는 돌지 않고 `verify.diagnose`를 STEP_SKIPPED로 기록한다. 취소·내부 실패 run에서는 부르지 않는다. 구현이 등록되지 않았으면 아무 기록도 남기지 않는다.
   - compare가 비교 불가(`failed`)인 것만으로는 돌지 않는다(G10 "기술적 비교 실패 뒤 diagnose로 불일치를 추정하지 않는다" 유지).
3. **실패·시간 초과**: diagnose 예외나 시간 초과(카탈로그 `timeout_s`, finally 보고처럼 run deadline과 별개)는 step 기록 `failed`와 `원인 분석 실패(판정 영향 없음)` 메시지로만 남긴다. run 상태는 바꾸지 않는다.
4. **입력**: `run_id`와 함께, 입력 모델에 선언된 필드만 실행기가 채운다(선언하지 않은 필드는 넣지 않으므로 `extra=forbid`와 충돌하지 않는다).
   - `reason`: `"parity_failed"` 또는 `"track_failed"`
   - `tracks`: 트랙 이름 → 상태(`build`·`local`·`cloud`·`verify`, 예 `"ROLLED_BACK"`)
   - `failed_steps`: 이번 run의 `failed`·`check_failed` step 목록. 항목은 `step_id`·`tool`·`target`(`"local"`/`"cloud"`/None)·`status`·`error`(실행기가 이미 redact한 메시지, check_failed면 None)·`output`(계약 모델을 통과한 툴 출력, 예외면 None)
   - 두 환경의 smoke 전체 결과는 지금처럼 `verify.smoke` 보관소에서 읽는다. call_ai가 입력을 다시 redact한다.
5. **출력과 보고 연결**: diagnose 출력은 after_step을 거치지 않으므로 RunContext를 바꾸지 않는다. 실행기는 출력을 run 기록(`steps["verify.diagnose"]`)과 STEP_FINISHED 이벤트로만 남긴다. `post_report`는 지금 RunContext만 받으므로 diagnose 출력을 직접 받지 않는다. 보고 카드에 원인 설명을 넣으려면 같은 verify 디렉토리 안의 보관소(smoke→compare 방식)나 별도 계약을 C3·O3가 정한다.

## 이유

판정은 결정적 검사(health·smoke·verify_tls·compare)만 한다. AI 설명은 사람이 원인을 이해하도록 돕는 부가 정보이고, 틀려도 배포 결과가 바뀌면 안 된다.
카탈로그 층 `builtin`은 원래 "실행기 내장, 계획에 안 나옴"이고 공통 계약 문서의 "내장(실패 시)"·"롤백 뒤 diagnose → post_report" 순서와도 맞다. 그래서 카탈로그·계약 스냅샷을 바꾸지 않았다.

## 반대 의견

없음(민영님 질문에 대한 답).

## 되돌리는 조건

diagnose 결과를 자동 조치(재시도·추가 롤백)에 쓰자는 결정이 나오면 다시 본다. 그때도 AI 출력이 아니라 규칙 범주(`rules.py`의 rule id)만 쓰는 쪽을 먼저 검토한다.

## 영향

- 대체되는 문구: [G10](2026-10-02-g-e2e.md)의 "compare/diagnose의 passed=False만 패리티 실패"는 "compare의 passed=False만 패리티 실패"로 읽는다. [야간 개발](2026-10-02-night-development.md)·[O1 가이드](../guides/O1.md)·O1 야간 인계서 4번의 같은 문구도 같다.
- 바뀐 코드: `src/ddak/executor/engine.py`(compare만 패리티 판정, diagnose 내장 호출, 계획 거부), 테스트 `harness/tests/unit/test_executor_diagnose.py`.
- 카탈로그·계약·이벤트 이름 변경 없음. `make contracts` 스냅샷 변화 없음.
- O3: diagnose 툴을 등록할 때 위 입력 필드 이름 중 필요한 것만 입력 모델에 선언하면 된다. 입출력 계약 모델(`core/contracts/tools`)은 O3 제안 뒤 contracts-update로 추가한다.

## 같은 작업: redact 빈틈 보강

PR #16 검토에서 재현된 `app.config["SECRET_KEY"] = "…"`(첨자 대입)과 `os.environ.get("SECRET_KEY", "…")`·`getenv("…_TOKEN", "…")`(비밀 이름 키의 기본값) 문자열을 `src/ddak/core/redact.py`가 가린다.
비밀 이름은 환경 키 분류 규칙(`plan/analyze/rules.py`)의 단어에 맞춘다(PRIVATE·이름 끝 `_KEY`는 대문자 설정 키만, `*_TOKENS`는 제외).
따옴표는 남기고 값만 `[REDACTED]`로 바꿔 줄 수를 유지한다. f-string·변수·환경 변수 참조·빈 값과 일반 키(PORT 등)는 가리지 않는다.
redact는 모든 AI 입력·로그에 쓰이는 공유 코드이므로 영향받는 담당자(O2 call_ai, O3 패치 생성, C3 화면)에게 알린다.
