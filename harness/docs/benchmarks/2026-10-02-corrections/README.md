# 수정 라운드 검증 — 2026-10-02

| 검증 | 결과 | 시간 |
|---|---|---:|
| 후보·서비스 초기 회귀 | 23 passed | pytest 14.71초 |
| 실행기·TLS·설정 회귀 | 137 passed | pytest 4.67초 |
| 독립 검토 반영 실행기·인프라 회귀 | 70 passed | pytest 2.94초 |
| 1차 전체 CI | 테스트 1,033 passed, lint 4건 실패 | 전체 58.253초 |
| 최종 전체 CI | **1,035 passed, 1 skipped, 4 deselected**, lint/type/boundary/contracts PASS | 전체 **56.495초**, pytest 54.25초 |

실제 VM·AWS·GitHub·Docker Hub 호출 없음. Git 후보 시험은 임시 로컬 bare 저장소다.
실제 Gitleaks CLI 시험은 이 Mac의 설치본으로 수행했다. 원격 quality job에는 미설치이므로
해당 실제 CLI 시험 2개가 skip되며 별도 secrets job의 Gitleaks Git 검사와 구분한다.
앱 전용 가상환경 시험 1개는 skip, Docker/AWS/LLM 등 별도 표식 시험 4개는 deselect다.

[UTC·경과 시간 CSV](validation.csv), [명령·종료 코드·로그 SHA256](validation.json)에
성공/실패 결과를 함께 보존한다. CI/pytest 시간은 실제 배포 속도가 아니다.
원본 로그는 ignored `harness/var/validation/correction-20261002/`에 남긴다.
공개 VM 벤치마크의 임시 주소 가림은 시간·컨테이너 ID·digest 측정값을 바꾸지 않았다.

[커밋 파일 목록](commit-files.json)은 현재 변경/신규 파일을 겹치지 않게 3그룹으로 나눈 것이다.
에이전트는 git stage·commit·push·PR을 수행하지 않았다. 이후 사람이 파일을 추가하면 목록도 재확인한다.
