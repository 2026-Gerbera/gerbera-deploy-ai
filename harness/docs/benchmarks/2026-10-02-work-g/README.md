# 작업 G 검증·시간 기록

실행 장소: 정준우 Mac의 로컬 작업 트리, 2026-10-02. 실제 AWS/GitHub/VM 접속 없음.
Git commit/push 동작은 임시 로컬 bare 테스트 저장소에서만 수행했다.

## 최종 CI

명령: `make -C harness ci`.

- **1,117 passed, 1 skipped, 4 deselected**. lint/type/boundary/contracts/test 모두 PASS.
- 전체 wrapper 시간 **74.628661초**, pytest 시간 **72.41초**.
- 시작 UTC `2026-10-02T04:32:48.151236+00:00`, 종료 UTC `2026-10-02T04:34:02.779611+00:00`.
- skip은 별도 temp-box 프로젝트에서 실행하는 Flask 앱 테스트 1개다. 외부 실행 테스트는 기본 CI 선택에서 제외된다.
- [final-ci.json](final-ci.json): 명령·UTC·실측. [ci.json](ci.json): 보존한 중간 CI 결과(실패도 포함).
- 로그는 ignored `harness/var/validation/work-g-20261002/`에 있다. 일부 초기 focused 로그는 재실행 때 덮어써졌으므로 모든 시도의 원본이 보존되었다고 주장하지 않는다.

## FAKE 왕복 리허설

명령: `make -C harness test ARGS='tests/e2e/test_g_roundtrip.py -q -s'`.
보고서는 [roundtrip.json](roundtrip.json)의 `source=fake`이며, 테스트 시작부터 보고서 생성까지 **4.622573초**다.

| 단계 | 요청 시작→릴리스 기록 | 승인 후→릴리스 기록 | 자동화한 승인 동작 |
|---|---:|---:|---:|
| v1 수동 | 1.640351초 | 1.490543초 | 0.001434초 |
| v2 이미지·박스 자동 감지 | 1.473569초 | 1.335010초 | 0.001872초 |
| v1 태그 재배포 | 1.190332초 | 1.158491초 | 0.001518초 |

전체에는 fixture 준비와 구간 밖 검증이 포함되므로 행별 합과 같지 않다(마지막 service.shutdown은 제외).
승인 시간은 테스트 코드가
approve를 호출한 시간이며 사람이 읽고 판단한 대기 시간이 아니다. v2에서 DB 스키마/auth 변경은 없다.
Git·계획·감시·승인·릴리스·태그/main 경로는 실제 코드를 사용하되 빌드/배포/검증 도구는 fake다.
따라서 수치는 컨트롤러·로컬 Git 참고값이며 실제 배포 3분 성공을 증명하지 않는다.

실제 계획에 필요한 미등록 툴: **build_image(C2), compare_env_results(O3), smoke_test(O3/C3)**.
전체 registry 누락과 실행기/스크립트 대체 항목은 JSON에서 별도로 구분한다. 운영상 추가 공백은
O2의 prod 감시 설정, O1의 운영 checkout 매핑, 생성기/binding 계약, 실제 외부 서비스 검증이다.

## 주요 보완과 검증

- G1/2: 감시 재시도와 준비 실패 기록, 설정 변경 경합, 재시작·승인 입력 변조 검사.
- G9: 마이그레이터 URL 분리, 빈 URL 차단, 파이프라인 SHA/이전 릴리스 복구.
- G3/4: 수동 ref 제한·태그 길이/네임스페이스, v1→v2→v1 후보·태그·main 검증.
- G8: 헤더 없는 hunk·모드/메타 거부, 실제 변경 경로 대조. 기존 O1 fixture 헤더도 정리.
- G5/6: 번들 누락 명시, 인프라 SHA 승인, 강제 포함/제외와 신규 migration만 실행.
  G5 연결 검사는 binding 조회를 대체하고 fake validate/plan을 등록한 회귀다. 실제 Terraform plan 통합 실행은 하지 않았다.
- G10: 비교 툴 오류/미등록은 FAILED_VERIFY, 롤백 없음, main 유지·성공 환경 태그 기록.

코드 변경 뒤 핵심 회귀를 추가하고 단계별 독립 검토를 받았다. 사용자 요청대로 TDD는 사용하지 않았다.
