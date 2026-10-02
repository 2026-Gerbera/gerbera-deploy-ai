# O1 fixture 리허설

`make -C harness demo`는 v1 초기 배포와 v2 패치 승인을 한 번 묻고
`DeploymentService.prepare → approval_view → approve → start → wait`로 실행한다.
`cloud/build/AI simulation, no real cloud`가 출력된다. 실제 이미지 빌드·클라우드·AI나
원래 sample-app 검증 결과가 아니다. 입력은 이 디렉토리의 `source/app.json`과 `v2.patch`다.

다른 시나리오는 `harness/`에서 다음 CLI를 사용한다. `--yes`는 fixture 테스트 전용이다.

```sh
uv run python scripts/dev.py demo --scenario parity_fail --yes
```

| 시나리오 | 실행 상태 | 최종 local/cloud |
|---|---|---|
| success | SUCCEEDED | v2 / v2 |
| local_fail | FAILED_LOCAL | v1 / v2 (local ROLLED_BACK, cloud SUCCEEDED) |
| cloud_fail | FAILED_CLOUD | v2 / v1 (local SUCCEEDED, cloud ROLLED_BACK) |
| parity_fail | PARITY_FAILED | v2 / v1 (local SUCCEEDED, cloud ROLLED_BACK) |

예상한 실패·롤백까지 일치하면 시나리오 CLI 종료 코드는 0이다. 실제 실행 상태는 별도 출력한다.
`harness/var/o1-demo/summary.json`에 source_files, 승인 snapshot, run ID, 상태와 시간이 남는다.
시간은 fixture 소요 시간이며 실제 3분 데모의 증거가 아니다. 기존 기록은 덮어쓰지 않는다.

다음 실행 전 `make -C harness demo-reset`으로 전용 fixture 상태만 초기화한다.
활성 컨트롤러, RUNNING/NEEDS_HUMAN, 미해제 잠금, 소유 불명 파일·링크는 거부한다.
일반 `var/`, 클라우드, Docker 볼륨·이미지는 삭제하지 않는다.
`uv run python scripts/dev.py demo-reset --containers`를 명시한 경우에만 로컬 Unix socket의
`ddak.demo=true`와 `ddak.project=ddak-o1-demo` 두 라벨을 가진 컨테이너를 재확인하고 삭제한다.
실 provider 테스트 자원은 해당 테스트가 직접 정리한다.
`make -C harness clean`은 캐시만 삭제하며 `var/` 전체(실행 DB·잠금·빌드 사본 포함)는
보존한다. fixture 초기화가 필요하면 `demo-reset`을 사용한다.

`make -C harness preflight`는 제한 시간 안에 Docker를 조회하고 실제 등록·미등록 도구와
담당을 출력한다. 자격증명·클라우드·AI 연결은 검사하지 않으며 누락이 있으면 코드 3이다.
`registry_missing`은 원래 레지스트리 미등록 목록이다. `implemented_internal`에는
`acquire_deploy_lock`의 Store/service 연결, `record_deploy_log`의 Store.finish,
로컬 CLI 전용 `preflight_check`, fixture 전용 `reset_demo_state`를 표시한다.
`missing`과 `missing_owners`는 이 네 내부 구현을 제외하며, 클라우드 운영 기능이
준비되었다는 뜻이 아니다. 카탈로그 kind와 스키마는 바꾸지 않는다.
`make -C harness demo-local`은 `DDAK_TEST_DOCKER=1`을 설정하고 `pytest -m docker tests/docker`를
실행한다. 실제 Docker 경로가 준비되지 않으면 코드 3이며 fixture로 대체하지 않는다.

패치는 공유 `core.snapshots.apply_diff`로 적용하며 CLI의 Git 환경 우회는 없다.
E2E는 실제 Git 저장소 안의 상태 경로에서도 v2 적용과 기존 기록 보존을 검증한다.
