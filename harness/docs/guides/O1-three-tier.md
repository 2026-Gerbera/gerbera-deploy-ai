# O1 — WSL2에서 WAS 시험 후 온프렘 3계층 배포

상태: **실제 VM WAS→3티어 첫 배포→v2→실패 롤백→DB 유지 재배포 검증 완료**(2026-10-02).
`reset_demo`의 3티어 초기화는 미지원이며 우회하지 않았다.
실측 진행 기록은 [VM 검증 보고](O1-vm-validation-2026-10-02.md)를 따른다.
아래는 사용자가 실행할 명령이다. 로컬 Docker 결과를 VM 성공으로 간주하지 않으며 3분 완료를 보장하지 않는다.
기준 CLI는 [o1_onprem.py](../../scripts/o1_onprem.py)의 `init/build/preflight/deploy`다.
10/2 야간 실행기는 환경 간 대기를 제거했고 run 대상 선택을 지원한다. 이 운영 CLI는
온프렘 단독·사전 빌드 산출물 경로이며 `deploy`의 이미지 확인·사람 승인을 유지한다.
Git 자동 후보/CodeBuild/클라우드 경로를 이 CLI 검증과 혼동하지 않는다. 아래 VM 실측은
야간 변경 전 코드 기준이고, 야간 이후 VM에는 접속하지 않았다.
현재 취합과 재검증 순서는 [아침 인계](O1-night-handoff-2026-10-02.md)를 따른다.

| 역할 | 주소 / 로그인 계정 **가정** | 진입점 |
|---|---|---|
| web | `192.168.10.4` / `server1` | cloudflared → nginx `:8080` |
| was | `192.168.10.2` / `server2` | Traefik `:8080` → 앱 3개, 내부 `:8000` |
| db | `192.168.10.3` / `server3` | 공식 MySQL `:3306`, named volume |

VM 콘솔의 `whoami`, `ip -br addr`로 대응을 먼저 확인한다. 다르면 inventory의 SSH 주소/계정,
DB URL, publish 주소, WAS_UPSTREAM, Traefik 신뢰 주소를 실제 값으로 함께 맞춘다.

## 1. WSL2와 빌드 환경

관리자 PowerShell([Microsoft 공식 설치](https://learn.microsoft.com/en-us/windows/wsl/install)):

```powershell
wsl --install -d Ubuntu
wsl --set-version Ubuntu 2
wsl --list --verbose
```

Docker Desktop에서 WSL2 엔진과 해당 Ubuntu의 WSL Integration을 켠다
([Docker 공식 안내](https://docs.docker.com/desktop/features/wsl/)).
검토된 현재 작업트리 사본을 WSL의 `$HOME/work/04_SoftBank_hackerton`에 준비한다.
미커밋 변경도 포함하되 다른 OS의 `.venv`, 앱 테스트 임시 디렉터리·DB는 가져오지 않는다.
키·env·state는 `/mnt/c` 대신 WSL Linux 파일시스템에 둔다.

```bash
sudo apt update
sudo apt install -y make curl git openssh-client jq nano
curl -LsSf https://astral.sh/uv/0.12.20/install.sh | sh
. "$HOME/.local/bin/env"
cd "$HOME/work/04_SoftBank_hackerton"
uv python install 3.13
uv sync --locked
# MySQL 공식 entrypoint가 init 스크립트를 source하도록 파일 모드를 보존
chmod 644 harness/apps/temp-box/docker/mysql/{ddak.cnf,10-init.sh,init.sql.template,ready.sh}
uv run python harness/scripts/o1_onprem.py --help
docker version
docker buildx inspect ddak-multi >/dev/null 2>&1 || docker buildx create --name ddak-multi --driver docker-container
docker buildx use ddak-multi
docker buildx inspect --bootstrap
```

예시 CLI는 로컬에서 사용한 uv 0.12.20으로 고정했다([공식 버전 지정 설치](https://docs.astral.sh/uv/getting-started/installation/)).
Builder의 Platforms에 `linux/amd64`, `linux/arm64`가 모두 있는지 확인한다.
Docker Desktop 엔진과 VM별 Docker Client/Server는 preflight 기준 API **1.49 이상**이어야 한다.

## 2. 각 VM 콘솔: Docker·SSH 준비

**server1/2/3 각각의 Ubuntu 콘솔**에서 사용자가 로그인해 실행한다. 기존 Docker가 정상이라면
설치는 생략하고 버전·권한만 확인한다. 아래는 신규 Ubuntu의
[공식 Docker apt 설치](https://docs.docker.com/engine/install/ubuntu/) 절차다.

```bash
whoami
ip -br addr
sudo apt update
sudo apt install -y ca-certificates curl openssh-server
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF_DOCKER
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF_DOCKER
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker ssh
sudo usermod -aG docker "$(id -un)"
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

마지막 **호스트 키 지문**을 VM별로 기록한다. 로그아웃 후 다시 콘솔 로그인하여
`docker version`이 sudo 없이 동작하는지 확인한다. Docker 그룹은 root 수준 권한이다
([공식 권한 안내](https://docs.docker.com/engine/install/linux-postinstall/)).

WSL에서 프로젝트별 키를 새로 만들고 공개키만 콘솔로 전달한다. 기존 키는 재생성하지 않는다.
암호를 설정했다면 CLI를 실행하는 같은 셸의 ssh-agent에 등록한다.

```bash
install -d -m 700 "$HOME/.ssh"
eval "$(ssh-agent -s)"
for project in flaskr-was flaskr-three; do
  ssh-keygen -t ed25519 -f "$HOME/.ssh/$project"
  ssh-add "$HOME/.ssh/$project"
  cat "$HOME/.ssh/$project.pub"
done
```

VM의 해당 계정 콘솔에서 WAS 시험 키는 server2에, three 키는 세 VM 모두에 등록한다.

```bash
install -d -m 700 "$HOME/.ssh"
read -r -p 'WSL 공개키 한 줄: ' DDAK_PUBLIC_KEY
printf '%s\n' "$DDAK_PUBLIC_KEY" >> "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
```

WSL에서 사용자가 `ssh -i "$HOME/.ssh/flaskr-was" -o StrictHostKeyChecking=ask server2@192.168.10.2`
등으로 실제 로그인·`docker version`을 확인한다. 첫 연결 지문은 **콘솔에서 기록한 지문과 같을 때만** 승인한다.
three 키로 세 계정을 각각 확인한다. inventory에는 같은 `SHA256:…` 지문과 키의 절대 경로를 넣는다.
CLI는 매번 keyscan 결과를 해당 지문과 비교하며, 지문이 바뀌면 자동 수락하지 않는다.

## 3. VM별 네트워크와 WAS Traefik

server2 콘솔:

```bash
docker network create flaskr-was-was-net
docker network create flaskr-three-was-net
read -r -p '승인 images.lock.json의 traefik.ref 전체: ' DDAK_TRAEFIK_IMAGE
run_traefik() {
  local project="$1"
  docker run -d --name "$project-traefik" --restart unless-stopped \
    --network "$project-was-net" -p 192.168.10.2:8080:8080 \
    -v /var/run/docker.sock:/var/run/docker.sock:ro "$DDAK_TRAEFIK_IMAGE" \
    --providers.docker=true --providers.docker.exposedbydefault=false \
    --providers.docker.network="$project-was-net" \
    --providers.docker.constraints="Label(\`ddak.project\`, \`$project\`)" \
    --entrypoints.web.address=:8080 \
    --entrypoints.web.forwardedheaders.trustedips=192.168.10.4/32
}
run_traefik flaskr-was
```

이미지 값은 WSL에서 `jq -r .traefik.ref harness/apps/temp-box/images.lock.json`으로 확인해 복사한다.
이미 있는 네트워크/컨테이너는 지우지 말고 `docker network inspect`/`docker inspect`로 소유·설정을 확인한다.
Traefik은 프로젝트 라벨과 네트워크로 시험을 분리한다. forwarded headers 신뢰 범위는 web VM 하나다
([Traefik 공식 설정](https://doc.traefik.io/traefik/reference/install-configuration/entrypoints/#forwarded-headers)).

server1(web) 콘솔:

```bash
# server1(web): 다른 네트워크와 subnet이 겹치지 않는지 먼저 확인
# 동적 할당 범위에서 cloudflared의 .2를 제외한다.
docker network create --subnet 172.30.10.0/24 --gateway 172.30.10.1 \
  --ip-range 172.30.10.128/25 flaskr-three-web-net
```

server3(db) 콘솔:

```bash
docker network create flaskr-three-db-net
```

VM 방화벽/상위 네트워크는 WAS `:8080`에 web VM, DB `:3306`에 WAS VM의 연결을 허용한다.
WSL의 SSH·검증 경로도 허용하되 Docker publish를 UFW 규칙만으로 차단했다고 가정하지 않는다
([Docker 방화벽 주의](https://docs.docker.com/engine/install/ubuntu/#firewall-limitations)).

## 4. WSL: WAS 단독 시험

이하 WSL 명령은 저장소 루트에서 실행한다. `--directory`와 `--output`은 **없는 경로**여야 한다.

```bash
export DDAK_STATE="$HOME/.local/state/ddak"
install -d -m 700 "$DDAK_STATE"
uv run python harness/scripts/o1_onprem.py init --directory "$DDAK_STATE/flaskr-was" \
  --layout was --base-url http://192.168.10.2:8080 --platform linux/amd64
nano "$DDAK_STATE/flaskr-was/inventory.json"
uv run python harness/scripts/o1_onprem.py preflight --directory "$DDAK_STATE/flaskr-was"
read -r -p 'Docker Hub 사용자명: ' DDAK_HUB_USER
docker login --username "$DDAK_HUB_USER"
uv run python harness/scripts/o1_onprem.py build --repository "$DDAK_HUB_USER/flaskr" \
  --output "$DDAK_STATE/flaskr-was/images-v1.json"
uv run python harness/scripts/o1_onprem.py deploy --directory "$DDAK_STATE/flaskr-was" \
  --artifacts "$DDAK_STATE/flaskr-was/images-v1.json" --mode bootstrap
curl -fsS http://192.168.10.2:8080/health/ready
curl -fsS http://192.168.10.2:8080/version
```

`init`은 `inventory.json`, `project.json`, `private/was.env`, `private/migrate.env`를 만든다.
SSH의 `REPLACE` 값들을 실제 키 경로/콘솔 지문으로 교체한다. 플랫폼은 VM 실측값으로 선택한다.
SQLite는 `/data/flaskr.sqlite`, 별도 named volume, 앱 ProxyFix는 `0/0`이다.
preflight 성공 후에만 다음 명령을 실행한다. CLI 승인 화면에서 이미지 digest를 확인한다.
기본 예시는 공개 Docker Hub 저장소를 전제로 한다. 비공개 저장소는 이 CLI를 실행하는
WSL의 Docker 로그인 상태를 사용해 원격 엔진에 pull을 요청한다. 실제 pull 성공을 확인하며
비밀 토큰을 명령 인자로 쓰거나 문서에 넣지 않는다.

## 5. WAS 통과 후 3계층 bootstrap

server2의 **같은 콘솔**에서 WAS 단독 Traefik만 중지하고 준비한 함수로 three Traefik을 시작한다.
두 Traefik이 같은 `192.168.10.2:8080`을 동시에 bind할 수 없다. WAS 시험 앱/SQLite는 보존한다.

```bash
docker stop flaskr-was-traefik
run_traefik flaskr-three
```

server1 콘솔에서 cloudflared를 **nginx와 같은 bridge의 전용 peer**로 먼저 시작한다.
공식 cloudflared 이미지는 별도로 pull한 뒤 실제 digest로 실행한다. 다른 컨테이너에 `.2`를 배정하지 않는다.

```bash
docker pull cloudflare/cloudflared:latest
DDAK_CLOUDFLARED_IMAGE=$(docker image inspect cloudflare/cloudflared:latest --format '{{index .RepoDigests 0}}')
docker run -d --name flaskr-three-cloudflared --restart unless-stopped \
  --network flaskr-three-web-net --ip 172.30.10.2 "$DDAK_CLOUDFLARED_IMAGE" \
  tunnel --no-autoupdate --url http://flaskr-three-web:8080
docker logs --tail 50 flaskr-three-cloudflared
```

HTTPS 주소가 출력될 때까지 로그를 확인한 뒤 그 공개 origin을 WSL의 다음 입력에 넣는다. nginx 배포 전 origin 오류는 가능하다.
[Quick Tunnel](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) 주소는 재생성 시 바뀔 수 있고 SLA가 없다.
발표용 고정 주소가 필요하면 [이름 있는 터널](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/)
설정을 먼저 확정하되 동일 bridge/IP와 nginx upstream을 유지한다.

```bash
read -r -p '실제 HTTPS 공개 origin: ' DDAK_PUBLIC_URL
uv run python harness/scripts/o1_onprem.py init --directory "$DDAK_STATE/flaskr-three" \
  --layout three --base-url "$DDAK_PUBLIC_URL" --platform linux/amd64
nano "$DDAK_STATE/flaskr-three/inventory.json"
uv run python harness/scripts/o1_onprem.py preflight --directory "$DDAK_STATE/flaskr-three"
uv run python harness/scripts/o1_onprem.py build --repository "$DDAK_HUB_USER/flaskr" \
  --three --output "$DDAK_STATE/flaskr-three/images-v1.json"
uv run python harness/scripts/o1_onprem.py deploy --directory "$DDAK_STATE/flaskr-three" \
  --artifacts "$DDAK_STATE/flaskr-three/images-v1.json" --mode bootstrap
curl -fsS "$DDAK_PUBLIC_URL/health/ready"
curl -fsS "$DDAK_PUBLIC_URL/version"
```

three의 `private/`에는 `db.env`, `web.env`도 생성된다. 전체 경로와 `state/`는 was 시험과 분리한다.
순서는 설정 주입 → 최초 DB 생성 → migration → WAS 3개 → nginx → health → 공개 HTTP smoke다.
앱 필수 설정/격리 테스트는 [앱 README](../../apps/temp-box/README.md)를 따른다. RELEASE_ID는 배포 run ID로 주입된다.
앱 pytest 임시 경로는 `harness/var/flaskr-pytest`를 사용한다.
앱 소스 안 `.pytest-tmp`의 심볼릭 링크를 snapshot에 포함시키지 않는다.

실제 경로는 cloudflared(`172.30.10.2/32`) → nginx(고정 공개 Host/Proto, XFF 재작성) →
`192.168.10.2:8080` Traefik(web `192.168.10.4/32`만 신뢰, XFF append) → 앱 ProxyFix `2/1`이다.
nginx listen/publish/ready 포트는 8080, WAS는 내부 8000이며 host port를 publish하지 않는다.
공개 URL로 회원가입·로그인·글 생성/수정/삭제와 Secure 쿠키를 직접 확인한다.

## 6. 업데이트와 보존

소스 변경 뒤 WSL에서 새 산출물 파일을 만든다. build 이후 소스가 바뀌면 deploy가 거부한다.
`--reuse-web`은 **이전 three 산출물 JSON**을 받는다. web Dockerfile, nginx/web 설정, static의 해시가
같을 때만 web 이미지를 재사용한다. 변경되면 자동으로 web도 빌드하며 WAS는 다시 빌드한다.

```bash
uv run python harness/scripts/o1_onprem.py build --repository "$DDAK_HUB_USER/flaskr" --three \
  --reuse-web "$DDAK_STATE/flaskr-three/images-v1.json" \
  --output "$DDAK_STATE/flaskr-three/images-v2.json"
uv run python harness/scripts/o1_onprem.py deploy --directory "$DDAK_STATE/flaskr-three" \
  --artifacts "$DDAK_STATE/flaskr-three/images-v2.json" --mode update
# 배포 CLI가 실행 중이지 않을 때, 비밀값을 포함한 전체 로컬 디렉터리를 비공개 백업
umask 077
install -d -m 700 "$HOME/ddak-backups"
tar -C "$DDAK_STATE" -czf "$HOME/ddak-backups/flaskr-three-$(date +%Y%m%d-%H%M%S).tgz" flaskr-three
```

`init`은 비밀번호 복구/회전 명령이 아니다. 기존 디렉터리를 지우고 다시 생성해 DB 비밀번호를
재발급하면 기존 MySQL 계정과 불일치한다. 분실/부분 생성 시 중지하고 원래 백업을 복구한다.
DB 컨테이너·named volume은 **삭제하지 않는다**. 고아 volume·기존 DB 이미지 변경·준비 실패는
자동 재생성하지 않고 수동 확인으로 중단한다. UPDATE 및 이전 DB 배포 기록이 있으면
DB가 없어도 신규 생성하지 않는다. WAS 교체 중 다른 정상 복제본이 없다면 마지막 정상
복제본을 내리지 않고 중단한다. 위 tar는 DB 데이터 백업이 아니므로 별도 일관된 DB 백업이 필요하다.
MySQL 앱 계정은 DML만, migrator는 별도 env의 해당 DB DDL 계정이며 앱에 전달하지 않는다.
공개 주소 변경도 init 재실행 대신 기존 inventory/public_env와 해당 HTTP env를 일치시킨 뒤 승인된 재배포로 처리한다.


VM 재부팅 뒤 컨테이너가 정지해도 파이프라인은 DB를 자동 재생성하거나 재시작하지 않는다.
진행 중이던 배포가 있었다면 먼저 실행 장부/잠금의 `NEEDS_HUMAN` 상태를 확인한다.
단순 재부팅으로 확인됐다면 db VM에서 사용자가 소유 라벨·volume 연결을 대조한 뒤
`docker start flaskr-three-db`로 **기존 DB만** 기동하고, WSL preflight 후 승인된 update를 실행한다.
자동 restart 정책은 현재 두지 않는다. 정지된 WAS/web은 그 배포 경로에서 기동·교체한다.

현재 readiness는 1초 간격 스키마 확인을 사용하는 데모 설정이다. 실제 부하·느린 DB에서의
probe 비용/오탐과 외부 IP 복원·HTTPS 쿠키는 VM 리허설에서 확인해야 한다.
오류 응답은 비밀값 노출 방지를 위해 원문 SQL/DSN을 내보내지 않는다.
