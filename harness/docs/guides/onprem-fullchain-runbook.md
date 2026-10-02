# 온프렘 풀체인 — 데스크탑 WSL 실행

10/3 수정 8 기준. 관리 웹·Claude·로컬 빌드는 **본인 데스크탑 WSL**에서 실행한다. VM은 web/was/db 배포 대상이다. 관리 웹을 VM·컨테이너·외부 바인딩으로 띄우지 않는다. 이 문서는 사람의 실환경 실행 순서이며 개발 검증은 가짜 runner/provider로만 했다.

## 1. 사전 준비와 확인

저장소 루트에서 실행한다. 사용자 셸에 다음 도구와 로그인이 준비돼 있어야 한다.

```sh
command -v uv git claude docker bash jq gitleaks
claude --help
docker version
docker buildx inspect --bootstrap
gitleaks version
```

- Claude는 같은 WSL 사용자로 로그인한다. `claude auth`의 상태 확인 명령은 설치 버전의 도움말에서 확인한다. `claude -p` 실호출은 운영자가 확인한다. 모델 기본은 Sonnet 5.5, 추론 강도 low이며 medium을 선택할 수 있다. 계정에서 해당 모델을 쓸 수 있는지는 이 개발에서 검증하지 않았다.
- 사용자가 미리 `docker login`을 한다. 제품은 토큰을 입력받거나 login을 실행하지 않고 기존 Docker CLI 설정을 쓴다. 저장소는 `2026gerbera/flaskr`다. push 권한은 실제 빌드에서 확인된다.
- 실행 중인 buildx builder가 linux/amd64·linux/arm64를 모두 지원해야 한다. QEMU·builder는 사람이 먼저 준비한다. 제품이 설치·생성하지 않는다. 사전 검사에서 확인 불가하면 승인 전에 멈춘다.
- **로그인 확인 제한:** 제품은 `docker system info`의 Username 메타데이터를 사용한다. 이 정보를 주지 않는 Docker/credential helper 구성은 로그인했어도 준비 단계에서 거부한다. 키 파일·토큰을 출력해 해결하지 말고 그 경우의 Docker 버전을 O1에 전달한다.
- 앱 저장소는 자격증명 없는 HTTPS URL, prod·ai-prod·main 브랜치가 필요하다. 현재 사용자 Git 신원·일반 push 권한·gitleaks가 준비돼 있어야 한다. prod는 PR merge만, ai-prod/main은 제품의 일반 push를 허용한다. 환경 기록은 deployed/* 태그다.
- 앱 소스의 `docker/web.Dockerfile`, `docker/was.Dockerfile`을 확인한다. 현재 플랫폼 buildspec은 이 경로를 쓴다. 앱의 임의 buildspec은 실행하지 않는다.
- [3티어 가이드](O1-three-tier.md)의 VM 키 로그인·호스트 지문·Docker·네트워크·인벤토리를 준비한다. 런타임 env와 마이그레이션 env는 분리된 0600 파일이다. 값은 코드·문서·로그에 넣지 않는다.

## 2. 환경변수 이름

값은 사용자 셸에 준비한다. 비밀값을 이 문서나 명령 인자에 쓰지 않는다.

| 이름 | 용도 |
|---|---|
| DDAK_ONPREM_INVENTORY | VM 인벤토리 파일의 절대 경로 |
| DDAK_WATCH_REPO_URL | 최초 앱 저장소 설정. `/ops`에서 저장해도 된다 |
| DDAK_WATCH_PROJECT / DDAK_WATCH_BRANCH / DDAK_WATCH_TARGETS | 저장 설정이 없을 때 프로젝트·브랜치·대상 보조 입력 |
| DDAK_BUILD_BACKEND / DDAK_IMAGE_REPOSITORY | 빌드 선택·Docker Hub 저장소 |
| DDAK_JEV_BACKEND / DDAK_LLM_BACKEND | 판단 역할·생성 역할 선택 |
| DDAK_LLM_MODEL / DDAK_LLM_EFFORT / DDAK_AI_TIMEOUT_S | Claude 모델·강도·호출 제한 시간(선택) |
| DDAK_ADMIN_PORT / DDAK_RUN_DIR / DDAK_SOURCES_DIR | 관리 웹 포트·상태/실행 기록·접수 사본 루트(선택) |

`onprem-run`은 미설정 항목에 real/local 빌드/claude-cli 판단/cli 생성/위 Docker Hub 저장소를 적용한다. 일반 제품의 기본 빌드는 CodeBuild다. 판단 Groq를 쓰려면 DDAK_GROQ_API_KEY·DDAK_GROQ_MODEL을 별도로 준비한다. DDAK_JEV_API_KEY는 TypeSafe 예약 키이며 현재 사용하지 않는다. Claude 리허설 때는 이 키를 unset하는 것을 권한다.

## 3. 관리 웹과 감시를 함께 시작

```sh
make -C harness onprem-run
```

기본 접속은 [온프렘 운영 페이지](http://127.0.0.1:8765/ops)다. 설정에서 앱 URL·prod 감시·onprem 대상·자동 감시를 저장한다. 설정 변경은 1초마다 감시 조립에 반영된다. 저장 설정이 환경변수보다 우선한다. 기존 demo 설정만 있으면 flaskr 요청은 기존 demo 기록을 사용한다.

**컨트롤러를 먼저 띄운 뒤 prod에 merge한다.** 첫 기동의 기존 HEAD는 기준선으로만 저장된다. 기존 HEAD를 지금 계획하거나 실패한 같은 커밋을 재요청하려면:

```sh
make -C harness onprem-plan PROJECT=flaskr
# 허용된 v* 태그 재배포 요청
make -C harness onprem-plan PROJECT=flaskr REF=v1
```

`/ops`의 ‘지금 계획 요청’ 버튼도 같다. 요청은 HTTP 202와 request_id·status_url을 즉시 돌려준다. 준비 상태 API를 조회하거나 자동 새로고침되는 `/ops` 목록에서 run_id와 승인을 확인한다. 같은 프로젝트·ref·대상의 준비 중 요청이나 AWAITING_APPROVAL은 재사용한다. 다른 ref/대상이 대기 중이거나 자동 계획이 준비 중이면 409로 거부하므로 먼저 기존 요청을 처리한다. 자동 감시끼리 겹친 준비는 순서대로 기다린다. 준비 task는 컨트롤러 프로세스 수명에 속하며, 완료 전 재시작하면 다시 요청한다. 준비 실패면 결과 기록에 phase/code/detail/missing_tool이 남는다.

## 4. 개발자 흐름

1. 앱 저장소에서 기능 브랜치의 변경을 PR로 prod에 merge한다.
2. 감시 → 분석·계획 → 승인 전 소스/gitleaks·빌더 검사가 끝나면 `/ops` 실행 목록의 승인을 연다.
3. 원본 SHA, 패치/인프라 메타, 미등록 환경 툴, 원본 fingerprint 예외 개수·규칙·경로를 확인하고 한 번 승인한다. 비밀값은 화면에 없다.
4. 제품이 후보 merge 커밋을 고정·일반 push하고 그 SHA를 임시 checkout해 buildspec으로 멀티 아키텍처 빌드·push한다. 온프렘 배포는 digest를 쓴다.
5. VM 헬스·스모크 후 결과/릴리스/환경 기록을 확인한다. 온프렘만 선택하면 클라우드 비교는 조건 미충족으로 생략된다.

시연에서는 승인 대기 중 다음 merge를 하지 않는다. 새 자동 run이 준비 완료되면 같은 프로젝트/ref의 이전 자동 AWAITING_APPROVAL run은 SUPERSEDED로 닫힌다. 수동·태그 요청은 보존한다.

## 5. 기존 DB를 사용하는 리허설 구성

O2의 현재 마이그레이션 검증은 DB 배포도 ‘마이그레이션 뒤’로 요구한다. 따라서 **이번 리허설 deploy.yaml에는 web·was만 넣고 이미 준비된 MySQL을 사용한다.** 인벤토리에는 DB 접속 정보가 남아도 되지만 DB deploy step을 생성하지 않는다. 첫 DB 준비까지의 자동 3티어 계획은 준석 확인 후 진행한다. DB·볼륨을 지우지 않는다.

분석이 `required(name)`로 읽는 키를 놓칠 수 있다. 필요한 비밀값은 기존 runtime env, 비밀이 아닌 설정은 인벤토리 public_env에 사람이 준비한다. public_env는 배포 argv에도 적용된다. `_MIGRATOR` 키는 런타임 env에 넣지 않는다. 마이그레이션 파일에는 별도 계정 URL을 넣고 제품이 일회성 컨테이너에 DATABASE_URL 별칭을 공급한다. RELEASE_ID·SOURCE_SHA는 제품이 공급한다.

**스모크 앱 계약도 필요하다.** 현재 검사는 `/version`의 release_id(run_id)·db.dialect(mysql), `/health/ready`의 status(ok), 게시판 목록/작성 동작을 요구한다. 저장소의 옛 temp-box는 이 응답과 맞지 않으므로 그대로 쓰면 실패한다. gerbera-application의 실제 배포 버전은 민영님이 확인해야 한다. 로그인 기능 추가는 이번 작업에 없다.

## 6. 잠금 해제와 실패 확인

실제 대상 상태를 운영자가 확인한 뒤 `/ops`의 해제 버튼 또는 다음 명령을 쓴다.

```sh
make -C harness unlock PROJECT=flaskr
```

실행 중인 run/살아 있는 task는 해제할 수 없다. 해제는 잠금·NEEDS_HUMAN 차단 면제와 감사 기록만 남긴다. NEEDS_HUMAN 결과·환경 원문을 성공으로 바꾸지 않으며 VM·DB를 조작하지 않는다. 새 실패가 나면 다시 차단된다. DIVERGED는 이 해제로 우회하지 않는다. 다른 컨트롤러가 살아 있으면 시작 lease에서 거부한다.

기본 `make` 진입점의 기록은 `harness/var/runs/<run_id>/`에 있다. context.json·plan.json·facts.json·JSONL 이벤트·최종 릴리스와 관리 페이지 결과를 본다. 상태 DB/감사 기록은 같은 var 루트다. `/ops/state`는 토큰 없는 잠금·blocked_targets·환경 상태를 반환한다.

실패 복구는 결과의 ROLLED_BACK 및 이전 이미지 관측값으로 확인한다. NEEDS_HUMAN이면 복구 성공으로 해석하지 않는다. v1 태그 재배포는 새 수동 승인 run이며 자동 롤백과 구분한다. 이미지/후보/실행 단계 시간은 기록으로 남긴다. 빌드 원문 로그는 비밀값 노출을 막기 위해 제품 오류에 싣지 않는다.
