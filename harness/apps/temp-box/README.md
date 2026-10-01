# Flaskr 배포 앱

[Pallets Flask 3.1.2 공식 tutorial](https://github.com/pallets/flask/tree/3.1.2/examples/tutorial)의
회원가입·로그인·로그아웃·게시글 CRUD 앱이다. 원본 템플릿과 CSS, BSD-3-Clause 고지를
보존했다. 원본/수정 구분과 다운로드 해시는 [NOTICE.md](NOTICE.md), [upstream/provenance.json](upstream/provenance.json)에 있다.

## 앱 환경

다음 환경변수는 전부 필수이며 앱은 기본값을 제공하지 않는다.

| 변수 | 의미 |
|---|---|
| `DATABASE_URL` | 로컬 `sqlite:////절대경로/flaskr.db`, 운영 `mysql+pymysql://flaskr_app:<주입값>@<DB주소>:3306/flaskr?charset=utf8mb4` |
| `SECRET_KEY` | 외부에서 생성·주입한 32자 이상 비밀값. 재시작/복제본 사이에는 같은 값 유지 |
| `APP_BASE_URL` | 실제 공개 origin. `http://localhost:8000` 또는 `https://<공개호스트>`, 경로·사용자정보 제외 |
| `SESSION_COOKIE_SECURE` | `https`는 `true`, 로컬 `http`는 `false`. origin 스킴과 일치해야 함 |
| `PROXY_FIX_X_FOR` | 신뢰할 `X-Forwarded-For` hop 수, 아래 경로별 설정 참조 |
| `PROXY_FIX_X_PROTO` | 신뢰할 `X-Forwarded-Proto` hop 수, 아래 경로별 설정 참조 |
| `RELEASE_ID` | `/version`이 `{"release_id":"..."}`로 반환하는 배포 식별자 |

앱은 `DATABASE_URL_MIGRATOR`를 받은 상태로 시작하지 않는다. MySQL 런타임 계정은
`flaskr_app`, DB는 `flaskr`로 제한한다. DDL은 앱 시작이나 HTTP 요청에서 수행하지 않는다.
`/health/ready`는 DB 연결, 스키마 버전 `0001`, 테이블·열·PK·고유키·외래키·생성시각 기본값을
검증해 준비되면 200, 불일치/접속 실패면 503을 반환한다. `/version`은 DB 장애 중에도 응답한다.
DB 예외 원문·연결 문자열은 HTTP/마이그레이션 출력에 넣지 않는다.

### 프록시 경로별 앱 환경

| 실제 요청 경로 | `PROXY_FIX_X_FOR` | `PROXY_FIX_X_PROTO` |
|---|---:|---:|
| WAS 직접 접속(로컬 HTTP) | `0` | `0` |
| cloudflared → nginx → WAS | `1` | `1` |
| cloudflared → nginx → Traefik → WAS | `2` | `1` |

마지막 경로는 nginx가 XFF를 클라이언트 IP 한 개로 재작성하고, Traefik이 nginx 주소를
한 번 append하며, nginx의 고정 proto 값을 유지한다는 전제다. Traefik의 forwarded headers
신뢰 대상은 nginx로 제한하고 WAS 직접 진입은 네트워크에서 제한해야 한다.
다른 진입구에는 그 경로에 맞는 설정을 별도로 주입한다. gunicorn 자체 forwarded-header
해석은 꺼 두어 ProxyFix 한 곳에서만 처리한다. 컨테이너는 8000 포트를 사용한다.

nginx 이미지에는 다음 변수를 필수로 주입한다.

| 변수 | 값 |
|---|---|
| `APP_BASE_URL` | 앱과 동일한 공개 origin |
| `PUBLIC_HOST` | APP_BASE_URL의 host[:port] |
| `PUBLIC_SCHEME` | APP_BASE_URL의 `http` 또는 `https` |
| `WAS_UPSTREAM` | 직접 WAS 또는 Traefik의 host:port, 예: `192.168.10.2:8080` |
| `TRUSTED_PROXY_CIDR` | 전용 cloudflared peer의 단일 IPv4 `/32` 주소 |

nginx는 8080 포트를 수신·노출한다. nginx 시작 전 origin 일치를 검사한다. `CF-Connecting-IP`는 지정한 cloudflared peer에서만
신뢰하며 XFF는 `$remote_addr`, proto는 고정 `PUBLIC_SCHEME`, Host와 forwarded host는
`PUBLIC_HOST`로 정규화한다. `/static/`은 nginx에서 직접 제공하고 나머지 경로(health/version 포함)는
WAS로 전달한다. 별도의 IPv6 peer 설정은 현재 제공하지 않는다.

### 마이그레이션/DB 환경

`python -m flaskr.migrate precheck|up|verify --json`은 성공/실패 모두 JSON 한 줄과
종료코드 0/1을 반환한다. 필드는 `phase`, `ok`, `current`(빈 DB는 null), `expected`(`0001`),
`applied`(이번 실행에서 적용한 버전 목록), `signature`(반영된 스키마의 SHA-256),
`fingerprint`(`version`, `sql_mode`, `collation`, `time_zone`, `ssl_version` 모두 문자열)다.
실패 시 `error= migration_failed`를 추가하며 비밀값을 포함하는 진단은 출력하지 않는다.

- `precheck`: 빈 DB 또는 완성된 `0001`만 허용하며 DDL을 실행하지 않는다.
- `up`: 빈 DB에만 초기 테이블과 버전 테이블을 생성한다. 기존 DB에서는 검증만 한다.
- `verify`: 이미 적용된 스키마를 읽어서 검증한다. 반복 실행으로 데이터/DDL을 변경하지 않는다.
- `DATABASE_URL_MIGRATOR`가 있으면 우선 사용한다. 없을 때 `DATABASE_URL` 대체 사용은 SQLite만 허용한다.
- MySQL은 `mysql+pymysql://flaskr_migrator:<주입값>@<DB주소>:3306/flaskr?charset=utf8mb4`를
  마이그레이션 프로세스에만 주입한다. APP_BASE_URL 등 HTTP 앱 환경은 CLI에 필요 없다.
- MySQL DDL은 암묵적 커밋이 있으므로 중간 실패 후 부분 스키마는 자동 복구/삭제하지 않고 실패한다.

공식 MySQL 이미지용 환경은 `MYSQL_DATABASE=flaskr`, 서로 다른 64자리 hex 값인
`DDAK_APP_PASSWORD`, `DDAK_MIGRATION_PASSWORD` 및 외부에서 주입한 `MYSQL_ROOT_PASSWORD`다. `MYSQL_RANDOM_ROOT_PASSWORD`는 공식
entrypoint가 생성값을 로그에 출력하므로 사용하지 않는다.
앱 계정은 DML만, migrator는 해당 DB의 DML 및 CREATE/ALTER/INDEX/REFERENCES만 받으며
DROP·전역 권한은 받지 않는다. 초기화 파일은 공식 entrypoint가 신규 빈 DB에서만 실행한다.
`10-init.sh`는 source 용도로 0644를 유지한다. `ready.sh`는 TCP `SELECT 1`로 임시 초기화 소켓과
구별하며 결과·비밀값을 stdout으로 내보내지 않는다. DB 커스텀 이미지는 만들지 않는다.

## 로컬 SQLite

저장소 루트에서 실행한다. 아래 비밀값은 실행 시 생성하며 파일에 기록하지 않는다.

```sh
uv sync --project harness/apps/temp-box --frozen
install -d -m 700 "$HOME/.local/state/ddak/flaskr-local"
export DATABASE_URL="sqlite:///$HOME/.local/state/ddak/flaskr-local/flaskr.sqlite"
export SECRET_KEY="$(uv run --project harness/apps/temp-box python -c 'import secrets; print(secrets.token_hex(32))')"
export APP_BASE_URL=http://localhost:8000
export SESSION_COOKIE_SECURE=false
export PROXY_FIX_X_FOR=0
export PROXY_FIX_X_PROTO=0
export RELEASE_ID=local-v1
uv run --project harness/apps/temp-box python -m flaskr.migrate precheck --json
uv run --project harness/apps/temp-box python -m flaskr.migrate up --json
uv run --project harness/apps/temp-box python -m flaskr.migrate verify --json
uv run --project harness/apps/temp-box gunicorn --bind 127.0.0.1:8000 \
  --graceful-timeout 25 --forwarded-allow-ips='' 'flaskr:create_app()'
```

브라우저는 `http://localhost:8000/`으로 연다. gunicorn은 SIGTERM을 받으면 진행 중 요청을
유예시간 내 마무리한다. 컨테이너 중지 유예시간은 gunicorn의 25초보다 길게 설정한다.
WAS 이미지의 `/data`는 UID/GID 65532 소유다. 볼륨을 덮어 마운트하면 해당 볼륨에도
같은 쓰기 권한을 제공해야 한다. 컨테이너 SQLite URL은 `sqlite:////data/flaskr.sqlite`다.

앱 전용 환경 검증:

```sh
uv run --project harness/apps/temp-box --frozen pytest \
  -c harness/apps/temp-box/pyproject.toml --confcutdir=harness/tests/unit/apps \
  --basetemp=harness/var/flaskr-pytest harness/tests/unit/apps/test_flaskr_app.py -q
```

## 이미지 빌드

승인된 공식 이미지 digest는 `images.lock.json`에 있으며 Dockerfile에 고정했다.
기본 `Dockerfile`은 `docker/was.Dockerfile`과 동일하다. 빌드 컨텍스트는 앱 디렉토리다.

```sh
docker build -f harness/apps/temp-box/docker/was.Dockerfile -t flaskr-was:local harness/apps/temp-box
docker build -f harness/apps/temp-box/docker/web.Dockerfile -t flaskr-web:local harness/apps/temp-box
```

의존성 변경 후 Docker 설치 잠금 파일을 갱신할 때:

```sh
uv lock --project harness/apps/temp-box
uv export --project harness/apps/temp-box --frozen --no-dev --no-emit-project \
  --output-file harness/apps/temp-box/requirements.lock
```
