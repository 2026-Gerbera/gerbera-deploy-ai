# O1 실제 VM 검증 — 2026-10-02

사용자 위임으로 WSL 및 web/was/db 시험 VM에서 실제 온프렘 툴 체인을 검증했다.
watch/intake/트리거 개편, AI 생성, AWS 배포는 제외했다. Git 커밋·push·PR은 하지 않았다.

| 단계 | 상태 | 근거·실측 |
|---|---|---|
| 인벤토리 모델 정합성 | DONE | deploy 공개 모델 재사용, VM 필드 및 MySQL 보호 회귀 |
| Mac·WSL CI | DONE | 각각 950 passed / 1 skipped / 4 deselected; 34.4초 / 26.8초, 최종 Mac 33.5초 |
| WSL 코드 동기화 | DONE | 깨끗한 main→o1/vm-test-20261002, diff 검사 후 적용; commit/push 없음 |
| WAS·3티어 preflight | DONE | 세 VM의 고정 지문·키/env 권한·API 1.56·amd64·network·소유 확인; three 5.728초 |
| 이미지 준비 | DONE | jungjoonwoo2030/gerbera-flaskr-was 및 -web; amd64/arm64 index·platform digest 봉인; DB 공식 이미지 |
| WAS v1 | DONE | 54.161초, SQLite migration·3복제본·실제 HTTP 검증 |
| 3티어 첫 배포 | DONE | 101.377초, DB→migration→WAS→web→health→공개 HTTPS smoke |
| 코드 변경 v2 | DONE | 75.195초, 실제 `/version` 변경, DB/web 컨테이너 유지 |
| 실패 주입·롤백 | DONE | 96.274초 중 rollback 26.875초, FAILED_LOCAL / ROLLED_BACK, v2 복구 |
| reset_demo | BLOCKED | flaskr-three allowlist 및 DB 인벤토리 미지원. 실제 거부·자원/장부 무변경 확인 |
| DB 보존 v1 복귀→v2 | DONE | 승인된 update로 73.128초 / 72.778초. reset_demo·DB 초기화 성공과 구분 |

[벤치마크 기록](../benchmarks/2026-10-02-onprem-vm/README.md)에 JSON·CSV·측정 조건·시험용 diff를 보존했다.
빌드는 미리 병렬로 수행했으므로 코드 변경부터 양쪽 환경 검증까지의 E2E 기록이 아니다.

## 최종 관측

- 공개 서비스: <<quick-tunnel-url>>. 임시 터널 재기동 시 주소가 바뀔 수 있다.
- 최종 v2 release: `onprem-1e4ae49d671b4fd39d6a6da5a9e4d1c8`.
- health/version/메인/static 모두 HTTP 200, health schema `0001`, WAS 3개+web+DB 모두 healthy, 잠금 0.
- 모든 후속 단계에서 DB/web 컨테이너 ID와 마운트는 첫 성공 배포와 동일했다. DB 볼륨을 삭제하지 않았다.
- 실패 시 마지막 성공 current/previous 및 기존 release manifest 해시가 보존됐다. WAS 1번만 v2로 복구하고 2·3번 ID는 유지했다.
- 네 업데이트·복구 구간 HTTP 표본 합계 565회, 오류 0회. 무중단/성공률 100%의 보장은 아니다.
- 비밀번호 로그인·Docker 토큰 조회·개인 키/env 내용 조회는 하지 않았다. 프로그램이 만든 private env는 기존 경로에 보존했다.

## 실행 위치와 명령

WSL 저장소 `/home/jjw/Desktop/gerbera-deploy-ai`, 브랜치 `o1/vm-test-20261002`.
uv는 기존 사용자 버전을 교체하지 않고 저장소 ignored `harness/var/tools/uv-x86_64-unknown-linux-gnu/uv`를 사용했다.

```bash
export PATH="$PWD/harness/var/tools/uv-x86_64-unknown-linux-gnu:$HOME/.local/bin:$PATH"
export BUILDX_BUILDER=ddak-vm-test
uv run python harness/scripts/o1_onprem.py preflight --directory /home/jjw/.local/state/ddak/flaskr-three
# 실제 사용한 v1 build 명령. 재실행은 새 --output 파일명 필요.
uv run python harness/scripts/o1_onprem.py build \
  --source /home/jjw/.local/state/ddak/vm-sources/v1 \
  --repository jungjoonwoo2030/gerbera-flaskr --three \
  --output /home/jjw/.local/state/ddak/flaskr-three/images-v1.json
# v2/broken은 각각 다른 source/output과 --reuse-web <이전 images JSON> 사용.
# deploy: --directory, --source, --artifacts, --mode bootstrap|update; 모든 실행 승인 기록 있음.
```

상세 실행 명령·전후 상태·시간은 `var/validation/vm-*.json/log`, 실행기의 `state/runs/<run_id>/events.jsonl`에 있다.
수집본 41개는 노트북 `harness/var/validation/vm-20261002/`에 보존했다. 비밀 파일을 증거 번들에 넣지 않았다.
Windows 원격 명령은 모두 base64로 감쌌고, 내부 SSH가 스크립트 stdin을 소비하지 않도록 했다.

WSL 상태는 `/home/jjw/.local/state/ddak/flaskr-{was,three}`, source 사본은 `vm-sources/{v1,v2,broken}`이다.
공식 저장소 앱 원본은 변경하지 않았다. init 재실행으로 비밀번호를 재생성하거나 private/state를 삭제하지 않는다.
WAS 단독 앱/SQLite, 중지된 WAS 시험 Traefik, 전용 네트워크, BuildKit, 실행 중 three 서비스·터널을 후속 시험용으로 유지했다.

## 남은 항목

- 3티어 전용 안전한 초기화 절차는 아직 없다. 현재 reset_demo를 우회하지 않았다.
- 업무 기능 v2·O3 패치·Git 감시·독립 클라우드 트랙 통합, 실제 사용자 IP 복원·인증 쿠키 E2E는 이번 결과에 포함하지 않는다.
- 초기 이미지 pull/캐시, 함께 실행 중인 시험 앱, 자동 승인, 병렬 빌드 등 비교 조건은 벤치마크 README를 따른다.

## 준석님 전달용 3줄

1. 인벤토리의 복제 모델을 deploy 공개 InventoryConfig/TierConfig 재사용으로 바꿔 3티어 필드 차이를 제거했습니다.
2. tier별 SSH·migration_env_file·ready를 수용하고 DB mysql+보호 볼륨 검증을 적용합니다. unix Docker 소켓 정책과 비밀 입력값 비노출은 유지합니다.
3. Mac/WSL CI 각각 950 passed이며 watch/intake·공유 계약은 변경하지 않았습니다.

제안 커밋:
- `fix(onprem): 인벤토리 검증을 3티어 배포 모델과 통일`
- `docs(onprem): 실제 VM 배포·롤백 검증과 벤치마크 기록`
