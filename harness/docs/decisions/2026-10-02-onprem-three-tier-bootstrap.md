# 2026-10-02 — 온프렘 WAS 시험과 3계층 최초 배포

후속 실행 결과(10/2 밤): [실제 VM 보고](../guides/O1-vm-validation-2026-10-02.md)와
[벤치마크](../benchmarks/2026-10-02-onprem-vm/README.md)를 따른다. WAS/3티어 배포·업데이트·롤백·DB 보존 재배포를 검증했다.
3티어 reset_demo는 아직 미지원이다. 아래는 최초 구현 시점의 계약·검증 기록이다.

상태: 구현 계약을 문서화했다. **실제 VM 로그인·배포·외부 경로 검증은 대기**다.
로컬 Docker에서는 SQLite WAS 단독 → MySQL 3티어 첫 배포 → WAS 업데이트를 검증했다.
WAS는 3복제본이며 업데이트 전후 DB·web ID를 보존했다. 실제 VM 성공을 뜻하지 않는다.
실행 절차는 [O1-three-tier 가이드](../guides/O1-three-tier.md)에 둔다.

## 범위와 배치

- WSL2 Ubuntu에서 현재 검토된 소스와 `harness/scripts/o1_onprem.py`를 사용한다.
  실제 CLI는 `init`, `build`, `preflight`, `deploy --mode bootstrap|update`이며 사람 승인을 유지한다.
- 먼저 `flaskr-was`로 WAS 3개/SQLite를 시험한 뒤 `flaskr-three`로 web/WAS/DB를 시험한다.
  두 프로젝트의 네트워크, named volume, private env, inventory와 state 디렉터리를 공유하지 않는다.
- 가정: web=`192.168.10.4`/`server1`, was=`192.168.10.2`/`server2`, db=`192.168.10.3`/`server3`.
  주소·계정·아키텍처는 사용자의 콘솔 및 실제 키 로그인으로 확인해야 한다.
- OS·Docker·SSH 키 등록·호스트 키 지문 확인·네트워크·Traefik·터널은 사용자 준비 범위다.
  preflight는 pinned SSH, Docker API 1.49 이상, 플랫폼, 네트워크, env 권한과 자원 소유를 확인한다.
  네트워크를 생성하거나 모든 공개 경로를 대신 검증하지 않는다.

## 프록시와 앱 계약

| 구간 | 확정값 |
|---|---|
| web bridge | `flaskr-three-web-net`, `172.30.10.0/24` |
| cloudflared | 같은 bridge의 고정 IP `172.30.10.2`, nginx 신뢰값 `172.30.10.2/32` |
| nginx | listen/publish/ready `8080`; upstream `192.168.10.2:8080` |
| Traefik | WAS VM `:8080`; trusted forwarded IP `192.168.10.4/32`; 프로젝트 라벨/네트워크로 제한 |
| WAS | 내부 `8000`, 직접 publish 없음; 3계층 ProxyFix `x_for=2`, `x_proto=1` |
| WAS 단독 | ProxyFix `0/0`, `sqlite:////data/flaskr.sqlite`, `/data`는 UID/GID 65532 쓰기 가능 |

nginx는 전용 cloudflared peer의 `CF-Connecting-IP`만 신뢰하고 XFF를 `$remote_addr`로 정규화한다.
Host/forwarded host는 APP_BASE_URL에서 파생한 PUBLIC_HOST, proto는 고정 PUBLIC_SCHEME다.
Traefik이 XFF에 한 hop을 append한다. 광역 trusted CIDR 또는 forwardedHeaders.insecure는 사용하지 않는다.
공식 근거: [nginx realip](https://nginx.org/en/docs/http/ngx_http_realip_module.html),
[Traefik forwarded headers](https://doc.traefik.io/traefik/reference/install-configuration/entrypoints/#forwarded-headers).

두 시험의 Traefik은 같은 WAS host port 8080을 사용한다. 전환 시 WAS 시험 Traefik만 중지하고
three Traefik을 시작한다. 기존 WAS 앱/SQLite나 MySQL을 삭제하는 전환 절차는 두지 않는다.

## DB·이미지·상태 보존

공식 MySQL digest 이미지를 사용하며 DB 커스텀 이미지를 빌드하지 않는다.
provider는 승인된 소스 사본의 다음 파일을 생성된 **정지 DB 컨테이너**에 복사하고 최초 start한다.

| 앱 소스 `docker/mysql/` | 컨테이너 경로 |
|---|---|
| `ddak.cnf` | `/etc/mysql/conf.d/ddak.cnf` |
| `10-init.sh` (0644, source) | `/docker-entrypoint-initdb.d/10-ddak-init.sh` |
| `init.sql.template` | `/opt/ddak-init.sql.template` |
| `ready.sh` | `/usr/local/bin/ddak-db-ready` |

공식 [MySQL 8.4 Dockerfile](https://github.com/docker-library/mysql/blob/master/8.4/Dockerfile.oracle)은
`/etc/mysql/conf.d/`를 생성해 include하고, [entrypoint](https://github.com/docker-library/mysql/blob/master/docker-entrypoint.sh)는
실행권한 없는 `.sh`를 source한다. 초기화는 신규 빈 DB에만 적용된다. readiness는 TCP `SELECT 1`이다.

`init`은 기존 경로를 덮어쓰지 않으며 DB 비밀번호를 복구하거나 재발급하지 않는다.
`flaskr_app`은 DML, `flaskr_migrator`는 별도 migration env로 해당 DB DDL을 수행한다.
앱 프로세스에 DATABASE_URL_MIGRATOR를 전달하지 않는다. DB 컨테이너/volume 삭제, 기존 DB 이미지 교체,
고아 volume 자동 재사용은 금지한다. UPDATE 또는 이전 DB 배포 기록이 있으면
컨테이너/volume이 모두 사라져도 새 DB를 생성하지 않는다. private env와 state를 포함한 디렉터리를 비공개로 백업한다.
DB 데이터의 일관된 백업은 그 디렉터리 백업과 별도로 필요하다.

사용자가 build 헬퍼로 amd64/arm64를 build/push하면 index와 플랫폼별 digest가 산출물 JSON에 기록된다.
`--reuse-web <이전 three 산출물>`은 web Dockerfile·nginx/web 설정·static 해시가 같을 때만
이전 web 이미지를 재사용한다. WAS 변경 뒤에도 전체 승인 snapshot은 새로 생성한다.
빌드 뒤 소스가 변하면 다시 빌드해야 한다. 앱 pytest basetemp는 `harness/var/flaskr-pytest`를 사용하여
앱 안 테스트 symlink로 snapshot 생성이 실패하는 일을 재발시키지 않는다.

## 검증 상태와 제외 범위

실제 VM의 키 로그인·Docker 권한·플랫폼, bridge 고정 peer, Traefik에서 관측되는 web 출발 IP,
공개 URL CRUD/Secure 쿠키, 업데이트·복구·DB 지속성은 실행 결과를 기다린다.
고정 MySQL 이미지에서 설정 include·계정 초기화·TCP readiness·C-09 migration을 실제 확인했다.
증거: `harness/var/validation/flaskr-three-tier-runtime.json`, `flaskr-docker-test.log`.

Quick Tunnel은 임시 주소와 서비스 제한이 있으므로 고정 공개 URL 및 데모 시간은 별도로 검증한다
([Cloudflare 공식 안내](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)).
3분 성공을 보장하지 않는다. 트리거 개편, 게이트 제거, 기존 담당자의 다른 문서 수정은 이번 범위가 아니다.
