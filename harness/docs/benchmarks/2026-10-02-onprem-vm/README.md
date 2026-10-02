# 온프렘 실제 VM 리허설 벤치마크 — 2026-10-02 KST

실제 WSL → VM 3대 → 공개 HTTPS 경로에서 얻은 **단일 리허설 측정값**이다.
AI 생성·Git 감시·AWS 배포는 포함하지 않는다. 실행기 결과의 빈 cloud 트랙 DONE을 클라우드 배포 성공으로 해석하지 않는다.

| 단계 | 배포 CLI 시간(초) | 결과 | HTTP 표본 / 오류 |
|---|---:|---|---:|
| WAS 단독 v1 | 54.161 | SUCCEEDED | 별도 지속 표본 없음 |
| 3티어 v1 첫 배포 | 101.377 | SUCCEEDED | 별도 지속 표본 없음 |
| 코드 변경 v2 | 75.195 | SUCCEEDED | 133 / 0 |
| readiness 실패 → v2 복구 | 96.274 | 예상된 FAILED_LOCAL / ROLLED_BACK | 173 / 0 |
| DB 유지 v1 재배포 | 73.128 | SUCCEEDED | 130 / 0 |
| v2 재배포 | 72.778 | SUCCEEDED | 129 / 0 |

실패 실행의 **롤백 구간은 26.875초**다(rollback.started/finished UTC 이벤트 차이).
각 배포의 전체 CLI 시간에는 uv/Python 기동·prepare·자동 승인·실행·결과 기록이 포함되고,
앞뒤 진단 수집 시간은 제외한다. 내부 step 37개의 elapsed_s는 별도 CSV에 있다.

| 별도 측정 | 초 | 조건 |
|---|---:|---|
| WAS amd64/arm64 OCI 빌드 | 41.967 | push 전 빌드 준비 |
| web amd64/arm64 OCI 빌드 | 4.065 | push 전 빌드 준비 |
| v1 빌드·Hub push | 39.313 | 위 OCI 빌드 캐시 사용; WAS+web |
| v2 빌드·Hub push | 9.328 | WAS 변경, web 재사용 |
| broken 빌드·Hub push | 7.895 | WAS 변경, web 재사용 |
| 3티어 preflight | 5.728 | 실제 세 VM, 지문 고정 SSH |
| Mac CI / WSL CI / 최종 Mac CI | 34.4 / 26.8 / 33.5 | 각 950 passed, 1 skipped, 4 deselected |

## 비교할 때 유지할 조건

- web/was/db 각각 amd64, 2 vCPU, 약 4GB RAM, Linux 6.8.0-139-generic, Docker 29.8.2/API 1.56.
- WSL2 kernel 6.6.87.2, 논리 CPU 16, Docker 29.8.2, uv 0.12.20. BuildKit 전용 builder `ddak-vm-test`, 내장 QEMU로 arm64 빌드.
- 서비스 WAS는 3복제본, 각각 gunicorn 2 workers, health 주기 1초. DB는 공식 MySQL digest 고정.
- WAS 단독 시험의 앱 3개는 이후에도 실행 중이고 그 Traefik만 정지했다. 따라서 3티어 구간의 WAS VM에는 서비스 3개 외에 유휴 시험 앱 3개가 함께 있었다. web의 기존 native cloudflared도 유지했다. 격리 벤치마크가 아니다.
- WAS 첫 배포는 최초 이미지 pull/SQLite 초기화. 3티어 첫 배포의 WAS 이미지는 캐시, MySQL/web은 최초 pull. 첫 v2/broken은 새 WAS 이미지 pull, v1 복귀/마지막 v2는 이미지 캐시 사용. 캐시 제거·호스트 정리 없음.
- v2/broken 빌드는 WAS 단독 배포와 병렬로 실행했다. **빌드 시간+배포 시간의 합은 연속 측정한 코드 변경→검증 완료 시간이 아니다.** 이 시험에서 3분 E2E나 성공률 100%를 입증하지 않았다.
- 사용자가 승인한 시험을 CLI stdin `y`로 진행했다. 사람의 판단/승인 대기 시간은 **미측정(null)**이다.
- 초기 WAS/3티어 CLI의 UTC 시작·종료는 별도로 수집하지 않았다(null). 대신 SQLite run 생성·종료 UTC와 CLI 실측 소요 시간은 보존했다. 이후 네 실행에는 CLI UTC도 있다. 추정값으로 빈칸을 채우지 않았다.
- HTTP 565회는 `/version`을 약 0.5초 간격으로 읽은 표본이다. 요청 사이의 장애나 모든 동시 사용자의 요청 성공을 보장하지 않는다. 개별 HTTP 응답 지연은 측정하지 않았다.
- v2는 `/version`에 `demo_revision=v2`를 추가한 시험 사본이다. broken은 `/health/ready`가 503을 반환하는 사본이다. O3의 업무 기능 v2 또는 AI 패치 성능 시험이 아니다.
- 실제 VM 실패 사례는 첫 WAS 복제본 실패다. WAS 2·3번을 유지하면서 1번만 복구했다. 두 번째 복제본에서 부분 실패하는 사례의 실제 VM 증명으로 확대하지 않는다.

## 보존 파일

- [vm-benchmark.json](vm-benchmark.json): 환경·소스/이미지 hash·run ID·UTC·구간 시간·step·한계.
- [vm-benchmark.csv](vm-benchmark.csv): CI/preflight/build/deploy 측정값.
- [vm-benchmark-steps.csv](vm-benchmark-steps.csv): 실행기 내부 step 시간 37개.
- [vm-benchmark-http.csv](vm-benchmark-http.csv): HTTP 표본 565개(상태·monotonic 시작 시각·관측 버전).
- [vm-variant-v2.patch](vm-variant-v2.patch), [vm-variant-broken.patch](vm-variant-broken.patch): v1 소스 기준 시험 사본 변경. 앱 원본에는 적용하지 않았다.
- [vm-final-assertions.json](vm-final-assertions.json), [vm-rollback-assertions.json](vm-rollback-assertions.json): DB/web ID·마운트, 기존 성공 기록, 정상 복제본, 잠금 해제 대조.
- [vm-reset-guard.json](vm-reset-guard.json): reset_demo 거부 및 무변경 확인. 3티어 초기화 성공 증거가 아니다.

상세 이벤트·단계 전후 상태·실행/빌드 로그 41개는 노트북 `harness/var/validation/vm-20261002/`,
WSL `/home/jjw/Desktop/gerbera-deploy-ai/harness/var/validation/`에 있다(ignored).
재실행 시 새 산출물 파일명을 쓰고 CLI의 기존 승인·잠금·digest·health 검증을 유지한다.
private env·state를 지우거나 재생성하지 않는다. DB 컨테이너·볼륨 삭제 금지.

현재 `reset_demo`는 프로젝트 allowlist와 DB 포함 인벤토리 보호 규칙 때문에 3티어를 거부한다.
이번에는 안전장치 거부를 확인한 뒤 **승인된 update로 v1 복귀→v2 재배포**를 검증했다.
DB 데이터 초기화 또는 reset_demo 완료로 기록하지 않는다.
