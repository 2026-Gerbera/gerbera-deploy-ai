# 후속 수정 2 검증 — 2026-10-02

| 검증 | 결과 | 측정 시간 |
|---|---|---:|
| 초기 후보·서비스 회귀 | 84 passed, 4 failed (테스트 준비 누락 후 교정) | 전체 32.389초 |
| 1차 CI | 1,051 passed, 1 skipped, 4 deselected | 전체 77.371초 |
| 템플릿 변경·삭제 보완 CI | 1,054 passed, 1 skipped, 4 deselected | 전체 77.669초 |
| 템플릿 추가까지 보완한 최종 CI | **1,055 passed, 1 skipped, 4 deselected** | **전체 88.544초**, pytest 85.78초 |

최종 lint·type·import 경계·계약 모두 통과했다. `make -C harness contracts-update`도 성공했으며
생성 스키마 변경은 없었다. 초기 실패는 새 테스트의 prepare_db 미등록 3건과 service 종료 전
재시작 1건이었다. 별도로 독립 검토에서 지적한 사용자 지정 템플릿의 승인본/후보 불일치를
교정하고 source 변경·patch 변경·삭제·추가 거부 4개 회귀로 prepare/supplied validate 양쪽을 확인했다.
코드·문서 독립 검토는 최종 PASS다.

[측정 JSON](validation.json)에 명령·시작/종료 UTC(수집된 항목)·경과 시간·종료 코드·로그 SHA256을 보존했다.
원본 성공/실패 로그는 ignored `harness/var/validation/followup2-20261002/`에 있다.
이는 로컬 단위·fixture·bare Git 검증이며 실제 배포 시간/성공률이 아니다.
실제 AWS·GitHub·VM·Docker Hub는 접속하지 않았다. 앱 전용 시험 1개 skip·별도 표식 4개 deselect는
이전 CI와 같은 조건이다. 원격 quality의 Gitleaks 설치 여부와 별도 secrets job은 이전 인계의 한계를 유지한다.

## 제안 커밋

1. `교차 검증 실패 게시를 차단하고 배포 후보 검사를 보완`: JSON의 code_files 6개 전체.
2. `앱 저장소 보호 규칙과 담당자 및 후속 수정 결과 정리`: 나머지 변경 Markdown·SVG와 이 벤치마크.

기존 3개 커밋은 사용자 반영 완료이며 이번 작업에서 stage·commit·push·PR은 하지 않았다.
