# verify/report

`post_report`는 파이프라인 안에서는 규칙 카드만 반환한다. 실행기가 run과 release를
확정하고 잠금을 해제한 뒤 같은 등록 툴에 구조화 사실을 넘겨 설명 요약을 만든다.
`call_ai`는 이 디렉토리에서만 호출하며, 입력은 값·로그·diff가 없는 허용 필드와
redact를 통과한다. 출력은 `ReportNarrative` JSON이다. 판정은 변경하지 않는다.

요약은 20초 제한, 재시도 없음. 실패·시간 초과·종료 시 규칙 요약을 사용한다.
`report-summary.json`은 배포 기록과 분리되며 결과 화면이 3초마다 조회한다.
출처는 source=ai 또는 rule, fake 어댑터는 실제 모델을 호출하지 않는다.
