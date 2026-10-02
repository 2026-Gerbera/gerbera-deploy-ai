# 후속 수정 3·4 측정 (2026-10-02)

모든 숫자는 **로컬 테스트/FAKE 외부 실행**이다. 실제 AWS·VM·LLM·Docker 빌드 및 사람 승인 시간을 포함하지 않는다.
UTC 시작·elapsed는 JSON을 원본으로 쓴다. pytest 숫자는 테스트 실행 시간이며 wrapper 시간과 구분한다.

| 검증 | 결과 | 측정 |
|---|---|---|
| 후속3 기반 1차 CI | 1124 passed, 1 skipped, 4 deselected | 74.340초 |
| 후속3 최종 코드 CI | 1146 passed, 1 skipped, 4 deselected | 78.341초 (pytest 75.61초) |
| 후속4 집중 1차 | 2 failed, 19 passed: 테스트 PlanStep 필수 필드 누락 | pytest 7.72초 |
| 후속4 집중 수정 | 21 passed | pytest 7.19초 |
| 후속4 CI 1차 | 2 failed, 1158 passed: 구형 수동태그 fixture 연결 누락 | 80.850초 (pytest 78.98초) |
| 후속4 독립 검토 반영 집중 | 48 passed | pytest 6.98초 |
| 마이그레이션 집중 | 28 passed, 120 deselected | pytest 0.26초 / wrapper 0.42초(서브에이전트 측정) |

`roundtrip.json`: annotated v1 수동 요청→서비스 재생성→승인→실행→v2 자동 감시→v1 수동 재배포.
실제 로컬 bare Git의 후보/태그/main 갱신을 검사하지만 build/deploy/verify는 테스트 가짜다.
총 4.499초, 요청→기록 v1 1.382초 / v2 1.435초 / v1 재배포 1.386초. 승인 대기는 코드가 즉시 수행한 수치다.
각 단계 승인 이후/요청 이후/승인 대기는 JSON에 따로 있다.

전체 원시 로그는 Git 제외 `harness/var/validation/followup3-20261002/`와 `followup4-20261002/`에 보존한다.
최종 전체 CI: **1164 passed, 1 skipped, 4 deselected**, 전체 **82.184초**, pytest **80.08초**. lint/type/boundary/contracts 통과.
별도 temp-box uv 프로젝트 테스트 1건 skip, 외부 Docker/AWS/LLM 마커 4건 제외. 이전 실패 기록도 보존한다.
