# 10/2 야간 로컬 검증 벤치마크

외부 VM·AWS·CodeBuild·AI·사람 승인 대기 시간은 **포함하지 않는다**. 테스트 수가 증가하는
개발 중 코드의 단일 실행 기록이므로 항목별 차이를 성능 개선/퇴보로 해석하지 않는다.
[CI CSV](ci-runs.csv)와 [JSON](ci-runs.json)은 실패 실행도 보존한다. 소요 시간은 CI의 합계이며
파일 mtime을 실제 명령 시작/종료 UTC로 표시하지 않았다. 원문 로그는 gitignored var/validation에 있다.

| 항목 최종 | 통과 | CI 합계(초) | 로그 |
|---|---:|---:|---|
| 1 | 953 | 33.4 | `item1-ci-final2.log` |
| 2 | 961 | 33.5 | `item2-ci-reviewed.log` |
| 3 | 975 | 36.8 | `item3-ci-reviewed.log` |
| 4 | 988 | 44.9 | `item4-ci-reviewed.log` |
| 5 | 1000 | 46.5 | `item5-ci-reviewed.log` |
| 6 | 1013 | 45.2 | `item6-ci-reviewed2.log` |
| 7 | 1015 | 45.7 | `item7-final-ci-reviewed.log` |

각 최종 실행은 1 skipped/4 deselected다. 작업7은 실제 Gitleaks 회귀 검사 2건을 포함한다. 최초 작업7은 테스트가 통과했지만 lint S311로 실패했고, 고정 가짜 토큰 생성식을 교정한 뒤 최종 전체 통과했다. 실패 기록을 성능 표본에서 지우지 않았다.

[최종 CI 전체 wall time/UTC](item7-final-ci-reviewed-timing.json), [이전 실패 실행](item7-final-ci-timing.json), [야간 소유 범위 검사](ownership-audit.json). CI 내부 합계는 반올림값이므로 Python 외부 타이머와 소폭 다를 수 있다.

[Gitleaks 실제 CLI fixture](gitleaks-real-fixture.json): 8.30.1, 안전 파일 통과/가짜 토큰 차단,
UTC와 perf_counter 소요 시간. 콜드/웜 캐시를 통제하지 않은 각 1회이며 탐지율 통계가 아니다.
비밀값·가짜 토큰 원문은 기록하지 않았다.

실제 온프렘 배포 시간은 [VM 벤치마크](../2026-10-02-onprem-vm/README.md)를 사용한다.
그 결과는 야간 코드 변경 전 실측이다. 새 실행기/후보 생성/CodeBuild 포함 3분 E2E는 별도 리허설이 필요하다.
