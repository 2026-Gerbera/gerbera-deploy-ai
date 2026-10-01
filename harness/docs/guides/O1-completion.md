# O1 완료 계획 (Codex 실행용, 2026-10-01)

> **10/2 추가 범위:** flaskr 최종형·tier별 이미지·DB→WAS→web 최초 배포를 추가했다. 이전 WAS 단독 범위는 [10/2 결정](../decisions/2026-10-02-onprem-three-tier-bootstrap.md)으로 정정한다. 실행은 [WSL2 가이드](O1-three-tier.md), 검증 상태는 [O1 가이드](O1.md)를 따른다.

> 1. 실행기·승인·잠금·스냅샷·컨테이너 모드 OnPremProvider는 이미 동작한다(`62dfcce`, `make -C harness ci` 382 passed). 남은 일은 온프렘 VM 대응과 데모 운영 도구다.
> 2. 순서: config·migrate 분리 → SSH 원격 Docker → app-1..3 순차 교체·롤백 → 설정·마이그레이션 → reset·preflight → 리허설 → (PR #1 merge 뒤) 웹 연결 → 실검증.
> 3. 공유 계약(스키마·카탈로그·RunContext·import 규칙)을 바꿔야 하면 그 항목만 멈추고 `NEEDS_CONTEXT`. 나머지는 이 문서의 기본안으로 진행한다.

- **목표 시한: 10/2(목) 밤.** 본선 10/3–10/4. 📌 표시(데모 핵심 경로)를 먼저 끝내고 남는 시간에 P1을 한다.
- 기준 순서(AGENTS §1): [10/1 기록](../decisions/2026-10-01-team-status-and-decisions.md) > [O1 착수 기준](../decisions/2026-09-30-o1-start-contracts.md) > dev-docs [00](../../../single-app/dev-docs/00_공통-개요.md) → [01](../../../single-app/dev-docs/01_공통-계약.md)(§3-7·§7-4·§11) → [02](../../../single-app/dev-docs/02_디렉토리-소유.md) → [roles/O1](../../../single-app/dev-docs/roles/O1_온프렘런타임-실행기.md) > [contracts README](../contracts/README.md) > [O1 가이드](O1.md) > 코드.
- 이 문서는 10/1 기록 D를 구체화한 실행 계획이다. 10/1 D에서 💭이거나 "정준우 확인"으로 남긴 것(권장 순서, D1 후속 P1 preflight의 순서)은 이 문서를 따른다. ✅ 결정 내용이 이 문서와 다르면 10/1 기록을 따르고 보고한다. ✅ 결정을 공유 계약 변경 없이 구현할 수 없으면(예: D4 ops 툴 위치와 import-linter 계약 3) 그 항목만 `NEEDS_CONTEXT`로 올린다.
- 이 문서와 10/1 기록은 아직 origin/main에 없다. 메인 작업 트리(로컬 파일)가 기준이다(§4 git 규칙).

## 실행 현황 (10/1)

WP1–9 구현·컨테이너 검증 완료, WP13 O1 가이드 갱신 완료. 검토 반영 후 CI 487 passed; 실제 Docker 서비스 경로 replicas=3·ports=[] 단일 장부 bootstrap→update×3→broken 복구→reset→재배포 통과. 상세 증거·소요 시간·코드 취합 질문은 [O1.md](O1.md).
WP10은 10/1 후속 사용자 지시로 임시 C-18 구조의 서비스 메타 입력·조회·승인 차단까지 구현했다(웹 제외). WP11은 PR #1 merge 대기, WP12와 실제 VM 확인은 외부 준비 대기다. temp-box 추가 범위는 v1 컨테이너 배포·헬스까지만이며 v2·실패 주입은 O3 산출물 뒤로 미룬다. Codex 완료와 O1 전체 완료를 구분한다.
추가 검증: 최신 CI 515 passed, PR #1 조합 528 passed. temp-box v1 실제 멀티 아키텍처 빌드→3 replica 배포·헬스 20.536초(캐시 사용), 양쪽 아키텍처 HTTP·SIGTERM 포함 Docker 테스트 1 passed/23.22초. 근거는 O1.md·`var/validation/temp-box-runtime.json`.

## 1. 완료의 정의 (DoD)

**데모 장면별 O1 보장 동작**

| 장면 | O1이 보장할 동작 | O1 단독 확인 | 팀 의존 |
|---|---|---|---|
| 0. v1 부트스트랩(데모 전 실행) | `mode=bootstrap` REAL run: `inject_env_config`(SECRET_KEY 없음) → `prepare_db`(0001) → `deploy_tier`(app-1..3 순차) → 검증 → `local_verified`. `env_release.local`에 REAL 성공 기록(rel-v1)과 원본 파일 해시 목록이 남는다. REAL update는 이 기록이 있어야 시작된다(`service.start`). | 가짜 runner 테스트, 컨테이너 모드 Docker 리허설, VM opt-in 리허설(WP9) | O3 flaskr·러너·health/smoke 등록, O2 계획, C2 산출물 |
| 1. v2 로그인 라이브 | SECRET_KEY는 v2 계획의 `keys`에 있을 때만 코드가 `token_hex(32)`로 생성·재사용한다(AI·화면·로그에 없음). Secure·ProxyFix는 `public_env` 값으로만 주입한다. 0002 마이그레이션, app-1..3 새 digest 순차 교체, replica마다 준비 확인, V20 관측(플랫폼 digest) 기록, rel-v2 기록. | 같음 | 같음 + C1 refresh(클라우드) |
| 2. 실패와 롤백 | replica k 준비 실패 → `deploy_tier` `ADAPTER_FAILED` → `rollback_tier`가 이전 digest와 다른 replica만 복구한다(멱등). 이전 기록이 없으면 소유 컨테이너를 제거한다. 클라우드는 `ABORTED_AT_GATE`, run은 `FAILED_LOCAL`. 복구 실패·종료 미확인(`ADAPTER_TIMEOUT`)은 `NEEDS_HUMAN`. | 가짜 runner (a)(b)(b')(c), 리허설 실패 주입(WP9) | - |
| 3. 교차 검증 불일치 | `PARITY_FAILED` → 클라우드만 롤백, 로컬 v2 유지, `DIVERGED` 기록(9/30 기준, 팀 정책 💭). 이미 구현됨. | 기존 테스트 유지 | O3 compare |
| 리허설 반복 | `reset_demo_state` 온프렘 몫으로 v1 상태 복구 → 다시 v2. 3회 연속. | WP8·WP9 | C2 클라우드 몫 |
| 교차 검증 기록 | tier별 관측 플랫폼·digest, `ctx.platform['onprem']['public_url']`, release manifest, JSONL 이벤트(seq, elapsed_s), 결과 dict `{status, tracks, steps}` 모양 유지. | 기존 테스트 + 새 테스트 | O3·C3가 읽음 |

**공통 조건**

- `make -C harness ci` 통과(기존 382개 유지 + 새 테스트). 원격 CI 3개(attribution·quality·secrets)는 정준우가 push·PR한 뒤 확인한다.
- 기존 컨테이너 모드 입력과 테스트가 무수정으로 통과한다(`mode` 생략 = container). 예외는 WP4 SECRET_KEY 기대값, 사용자 확정 local health 구현, 10/1 검토 반영의 rollback RELEASE_ID·env 키 판정 기대값이다.
- 비밀값과 SSH 키 내용이 로그·이벤트·`context.json`·승인 화면·AI 입력·작업 보고 어디에도 없다. 키는 경로만, env 파일은 키 이름만 다룬다.
- 실행·롤백·잠금 코드는 LLM을 부르지 않는다(import-linter 1). 툴 디렉토리끼리 import하지 않는다(계약 3).
- 보고는 `DONE`/`NEEDS_CONTEXT`/`BLOCKED`(AGENTS §5). 팀 의존 때문에 증명하지 못한 항목은 `BLOCKED: <담당> <산출물>`로 남긴다.
- **Codex 완료(10/2 밤)** = 가짜 runner 테스트 전부 + 컨테이너 모드 실 Docker 리허설(부트스트랩 1 + update 3회 연속 + broken 이미지 실패 주입 1 + reset 후 재실행) + `make -C harness ci`.
- **O1 완료** = Codex 완료 + VM 리허설 1회(정준우의 VM·이미지 준비 알림 뒤). VM·이미지가 없거나 Codex 실행 환경(sandbox)에서 Docker 소켓·네트워크가 막히면 그 항목만 `BLOCKED: <원인>`으로 남기고, 정준우가 직접 돌릴 명령(예: `DDAK_TEST_DOCKER=1 make -C harness test-docker`)을 보고에 적는다. 팀 의존 항목은 연결 지점(시그니처·fake)이 준비돼 있고 남은 의존이 기록된 상태로 둔다.

## 2. 작업 패키지 (WP)

크기(Codex 기준 추정): S ≤2h · M 2–4h · L 4–6h. 📌 = 데모 핵심 경로. 파일은 O1 소유만 적었다.

### WP1 📌 config·migrate 분리 — `o1/onprem-split`, S
- 목적: VM 작업 전에 동작 변경 없이 파일을 나눠 같은 코드를 두 번 흔들지 않는다(10/1 D3).
- 파일: `src/ddak/onprem/deploy/{provider,config,migrate,__init__}.py`. O1.md 경로 수정도 로컬에서 바로 한다(§4).
- 구현: `_private_env`와 `inject_config` 본문 → `config.py`. `migrate_db` 본문, `_MigrationResult`, `_Fingerprint` → `migrate.py`. provider 메서드는 한 줄 위임만 남긴다. `OnPremProvider`는 `provider.py`에 둔다(`test_onprem_deploy_skeleton.py`가 `OnPremProvider.__module__ == "ddak.onprem.deploy.provider"`와 config·migrate의 `__doc__`를 검사한다). 공개 이름(`DockerHost`, `OnPremProvider`), 오류 메시지, ErrorCode는 그대로 두고, `onprem/deploy/__init__.py` docstring의 "(지금은 비어 있음)"만 고친다.
- 확인: `harness/tests/unit/onprem/deploy/test_onprem.py`(테스트 함수 22개, 파라미터 포함 46개)와 `test_onprem_deploy_skeleton.py`가 무수정으로 통과(테스트를 고쳐야 하면 동작이 바뀐 것이다). `make -C harness ci`.
- 막히면: 해당 없음(O1 파일만).

### WP2 📌 VM 인벤토리 + SSH 원격 Docker — `o1/onprem-vm`, M
- 목적: was VM의 Docker를 `ssh://`로 원격 호출한다(10/1 B5·B6·D1).
- 파일: `onprem/deploy/{provider,containers}.py`, 새 `onprem/deploy/ssh.py`, 새 테스트 `harness/tests/unit/onprem/deploy/test_onprem_vm.py`.
- 구현:
  - 입력 모델(provider 소유, 전부 선택 필드, 이름 💭): `mode: container|vm`(기본 container), `public_url`, `ssh{host, user, port=22, key_path, host_key_fingerprint, jump_host?}`, `jump_host{host, user, port=22, host_key_fingerprint}`. tier에는 `replicas`(1–5, 생략 가능), `traefik_labels`(키는 `traefik.`로 시작, 값에 개행·NUL 금지), `ready{port, path, timeout_s}`. 지문은 `ssh-keygen -lf` 출력 형식(`SHA256:<base64 43자>`)만 받는다. host는 호스트명·IP 정규식으로 엄격히 검사한다(점프 호스트의 원격 명령 문자열에 들어간다). `extra=forbid`·strict는 유지한다. dev-docs 01 §3-7은 jump_host를 "주소"로만 적었으므로, 작업 보고에 필드 표를 적어 김준석(C-04)에게 알린다.
  - vm 모드: `ssh` 필수, `docker_host` 직접 지정 금지, `ports`는 빈 목록(Traefik이 내부 라우팅). endpoint는 코드가 만든 `ssh://<alias>`(예: `ddak-target`)이고 user·port는 ssh_config에 둔다.
  - 키 파일: `stat`만 한다(존재, 일반 파일, 그룹·기타 권한 없음, 소유자=현재 uid). 내용은 열지 않는다. 위치는 사람이 정한다(권장: `~/.ssh` 밖 전용 경로, 0600, passphrase 없음 또는 ssh-agent. BatchMode라 입력을 받지 못한다).
  - SSH 설정: 사용자 `~/.ssh`는 읽지도 고치지도 않는다. provider 메서드 호출 1회(deploy·rollback·migrate_db)마다 `tempfile.mkdtemp(prefix="ddak-ssh-")`(0700)에 `ssh_config`·`known_hosts`(0600)를 만들고, 끝나면(예외 포함) 지운다. 10/1 기록의 `var/`는 예시이고 provider는 앱 상태 경로를 모른다. docker를 부르지 않는 `inject_config`는 SSH를 준비하지 않는다. `ssh_config`는 `Host <alias>` 블록 하나: `HostName`, `Port`, `User`, `HostKeyAlias <alias>`, `IdentityFile`=key_path, `IdentitiesOnly yes`, `StrictHostKeyChecking yes`, `UserKnownHostsFile`=생성 파일, `GlobalKnownHostsFile /dev/null`, `BatchMode yes`, `ConnectTimeout 10`, `ServerAliveInterval 10`. `ControlPersist`는 쓰지 않는다(백그라운드 master가 docker CLI의 stderr 파이프를 잡아 명령이 끝나지 않는다).
  - 호스트 키: `ssh-keyscan -T 5 -p <port> <host>`를 runner로 실행하고, 각 키의 지문은 파이썬으로 계산한다(`"SHA256:" + base64(sha256(blob)).rstrip("=")`. ssh-keygen과 stdin이 필요 없다). 인벤토리 지문과 같은 키 한 줄만 `<alias> <type> <blob>`로 known_hosts에 쓴다. 일치하는 키가 없으면 `DdakToolError(CONFIG_INVALID, "호스트 키 불일치")`로 즉시 중단한다. 어떤 경우에도 `StrictHostKeyChecking no`·`accept-new`를 쓰지 않는다.
  - jump_host(완료 기준. 10/1 D1 (e), dev-docs 01 §3-7 "같은 코드가 동작"): 대상 블록에 `ProxyJump <jump-alias>`를 두고, 점프 호스트도 자기 `Host` 블록과 위 지문 대조로 known_hosts에 고정한다. `ssh-keyscan`은 ProxyJump를 쓰지 못하므로 대상 호스트 키는 지문을 먼저 확인한 점프 호스트에서 받는다: `ssh -F <cfg> <jump-alias> -- ssh-keyscan -T 5 -p <port> <host>` → 로컬에서 지문 대조 → 일치한 줄만 기록.
  - docker CLI에 설정 전달: `Runner` 프로토콜 `(argv, *, timeout)`은 바꾸지 않는다(`test_onprem.py`의 가짜 runner가 이 시그니처다). env 변경은 vm 모드에서만 한다. 기본안은 argv 앞에 `/usr/bin/env -u DOCKER_HOST -u DOCKER_CONTEXT PATH=<래퍼 디렉토리>:<원래 PATH>`를 붙이는 것이다(가짜 runner로 확인할 수 있다. vm 전용 runner로 해도 된다). HOME·DOCKER_CONFIG·SSH_AUTH_SOCK은 그대로 둔다. credential helper가 이 값을 써야 노트북의 Docker Hub 자격이 원격 pull에 실린다. 래퍼 `ssh`는 `exec <shutil.which("ssh")> -F <cfg> "$@"` 한 줄(0700)이다. 레지스트리 조회(`buildx imagetools inspect`)는 `--host` 없이 노트북에서 한다.
  - `DockerHost`: 생성자에 명시적으로 넘긴 `ssh://` endpoint만 추가로 허용한다. 환경변수 `DOCKER_HOST`의 ssh·tcp 값은 계속 거부한다.
  - 오류: docker CLI는 ssh 실패도 exit 1로 감싸므로 구분할 수 없다(지금처럼 `ADAPTER_FAILED`). 그래서 호출마다 docker 명령 전에 1회 `ssh -F <cfg> -- <alias> true`로 접속을 확인한다. 직접 부르는 `ssh`·`ssh-keyscan`의 종료 코드 255나 시간 초과는 상태 변경 전이므로 `ADAPTER_FAILED("SSH 접속 실패")`로 낸다(새 ErrorCode 없음). detail에는 필드 이름만 쓰고 키 경로 같은 절대 경로는 넣지 않는다.
  - provider docstring의 "원격 SSH/TCP daemon… 아직 지원하지 않는다"를 고친다.
- 확인: 가짜 runner로 (d) SSH 실패·호스트 키 불일치 시 중단, (e) jump_host: 점프 호스트 경유 keyscan argv, `ProxyJump`, 점프·대상 지문 모두 대조. 생성 파일 0600·디렉토리 0700과 삭제(예외 포함), vm argv에서 `DOCKER_HOST` 제거, 키 파일 open 호출 없음. 컨테이너 모드 테스트 무수정 통과. `make -C harness ci`.
- 막히면: 필드 모양(C-04)은 기본안으로 구현하고 작업 보고에 적는다(10/1 D1 예외). `core/contracts`·RunContext를 바꿔야 하면 `NEEDS_CONTEXT`.

### WP3 📌 replica 순차 교체 + Traefik + 준비 확인 + 롤백 — `o1/onprem-vm`, L
- 목적: app-1..3을 새 digest로 하나씩 교체하고 실패하면 이전 digest로 되돌린다(B5, dev-docs 01 §11-2).
- 파일: `onprem/deploy/{provider,containers}.py`, 새 `onprem/deploy/replicas.py`(선택), 새 `onprem/deploy/health.py`(위임 자리), `test_onprem_vm.py`.
- 구현:
  - 이름·라벨: `replicas`를 지정하면 replica 이름은 `<tier.name>-<i>`(i=1..replicas, WP6 규칙). 라벨은 기존 `ddak.managed`·`ddak.project`·`ddak.tier`에 `ddak.replica=<i>`를 더한다. Traefik 라벨(인벤토리 `traefik_labels` + 코드가 붙이는 `traefik.docker.network=<tier.network>`)은 app replica(`<name>-<i>`와 그 `-ddak-next`)에만 붙인다. 마이그레이션 컨테이너에 붙으면 Traefik이 그쪽으로 요청을 보내므로 라벨 인자를 `_args`와 분리한다. `traefik_labels`에 `traefik.docker.network`가 있으면 `CONFIG_INVALID`. network는 Traefik과 공유하는 것 하나만 쓴다(provision이 준비).
  - deploy: tier당 pull과 manifest·플랫폼 digest 검증은 1회(기존 `_manifest`·`image`). 그다음 i=1..N 순서로 기존 `_replace` 흐름(`-ddak-next` 생성 → 기존 제거 → rename → start) → 준비 확인 → 다음 replica. 준비 확인에 실패하면 바로 `DdakToolError`를 내고 남은 replica는 건드리지 않는다.
  - 준비 확인(O1 몫): `docker inspect`로 Running과 RestartCount 0을 확인한다(`_CONTAINER_FORMAT`에 RestartCount 추가, Env는 계속 요청하지 않는다). 이어서 `docker exec <replica id> python -c <코드에 고정한 스니펫> <대기 초>`를 replica당 1번 실행한다. 스니펫은 컨테이너 안에서 `http://127.0.0.1:<ready.port><ready.path>`를 0.5초 간격으로 폴링해 200이면 exit 0, 대기 시간이 지나면 exit 1이다. exec 명령 timeout은 대기 + 10초, `shell=True` 금지. 일회성 프로브 컨테이너는 쓰지 않는다(잔여 컨테이너·DNS·SSH 왕복이 늘어난다). 기본값은 port 8000, path `/health/ready`(dev-docs 01 §8-5, O3 확정 전 💭), timeout_s 30.
  - 시간 예산: 카탈로그의 `deploy_tier`·`rollback_tier` `timeout_s`는 300초이고, 넘으면 실행기가 `ADAPTER_TIMEOUT`(종료 미확인)으로 처리한다. pull(명령당 최대 60초)과 replica 3개 × (교체 + 준비 대기)가 이 안에 들어가야 하므로 replica별 대기는 `min(ready.timeout_s, ctx.deadline - now - 30)`으로 자르고 tier 전체를 `ctx.deadline` 안에서 배분한다. `timeout_s` 변경은 카탈로그 변경이라 `NEEDS_CONTEXT`.
  - 오류 코드(장면 2 핵심): 실행기는 상태 변경 툴의 `ADAPTER_TIMEOUT`을 종료 미확인으로 보고 롤백 없이 `NEEDS_HUMAN`으로 잠금을 남긴다(`executor/engine.py`의 `unquiesced`). 그래서 준비 실패(docker 명령은 정상 종료했는데 앱이 200을 못 줌)는 반드시 `ADAPTER_FAILED`다. 컨테이너를 바꾸기 전 단계(SSH 확인·pull·manifest·inspect)의 시간 초과도 대상이 그대로이므로 `ADAPTER_FAILED`로 낸다. `ADAPTER_TIMEOUT`은 create·rm·rename·start 이후 명령의 시간 초과에만 쓴다. 롤백도 같은 준비 확인을 하고, 롤백 중 준비 실패는 `ADAPTER_FAILED`다(실행기가 `ROLLBACK_FAILED` → `NEEDS_HUMAN`으로 처리).
  - V20: tier당 1회 `image inspect --platform`과 manifest로 얻은 플랫폼 digest가 `release_artifacts.images[tier].platform_digests[<tier platform>]`과 같은지 확인한다(기존 `_replace`와 같은 일치 비교). replica마다는 컨테이너 inspect로 `host.matches`를 확인한다. `observation`은 tier당 1개.
  - rollback(멱등): 소유 라벨로 replica를 조회해 이전 digest(`previous_release['local']['images'][tier]`)와 다른 것만 같은 절차로 교체하고, 없는 번호는 새로 만든다. 교체 도중(기존 제거 뒤, rename 전) 실패해 `<name>-<i>-ddak-next`만 남은 경우도 staged를 지운 뒤 이전 digest로 새로 만든다. 이전 기록이 없으면 그 tier의 소유 replica와 `-ddak-next`를 모두 제거한다. detail에 복구한 번호를 남긴다.
  - 소유 라벨이 다른 같은 이름 컨테이너는 건드리지 않고 기존 `DockerHost.owned`(`containers.py:161`)처럼 `PRECONDITION_FAILED`. Traefik은 재시작·재구성하지 않고 라벨만 붙인다.
  - health_check 위임: 생성자의 `health_checker` 주입을 유지하고(`test_onprem.py:427`), 주입이 없으면 `onprem/deploy/health.py` 함수에 위임한다. 사용자 확정으로 O1이 local health를 구현한다. 관측 실패는 passed=False, SSH·CLI 오류는 예외로 유지한다.
- 확인: 가짜 runner로 (a) app-1→2→3 순서와 단계별 준비 확인, (b) app-2 실패 시 rollback이 app-1·app-2만 복구하고 app-3은 그대로, (b') app-2 기존 제거 뒤 실패 → rollback이 app-2를 이전 digest로 새로 만든다, (c) 이전 기록 없음 → 소유 replica 제거, rollback 연속 2회 중 두 번째는 `changed=False`. 준비 실패 → run `FAILED_LOCAL`·local `ROLLED_BACK`(`NEEDS_HUMAN` 아님). 마이그레이션 컨테이너에 Traefik 라벨 없음. `make -C harness ci`.
- 막히면: Traefik 라벨 키·network 이름은 인벤토리 값으로 받는다(💭 김준석). 준비 경로·포트도 인벤토리 값(💭 장민영).

### WP4 📌 설정·시크릿 주입 — `o1/onprem-vm`(커지면 `o1/onprem-config`), S
- 목적: v2 로그인에 필요한 설정을 비밀값 노출 없이 주입한다(B8, O3 env 키).
- 파일: `onprem/deploy/{config,provider}.py`, 테스트.
- 구현:
  - env 파일 위치는 **A안(파이프라인 노트북의 파일 + `--env-file`)** 을 유지한다(10/1 D1·E). 원격 파일 쓰기 코드는 만들지 않는다. B안(was VM 호스트 파일)은 결정이 나면 별도 WP로 한다.
  - `public_env` 허용 목록에 `APP_ENV`, `PROXY_FIX_X_FOR`, `PROXY_FIX_X_PROTO`를 더한다. 값 검사: PROXY_FIX는 0–5 정수, `SESSION_COOKIE_SECURE`는 true/false. hop 수를 코드에 박지 않는다.
  - 일관성: `SESSION_COOKIE_SECURE=true`인데 `public_url`이 http면 `CONFIG_INVALID`(로그인 깨짐을 미리 막는다).
  - `RELEASE_ID`: deploy 때 `-e RELEASE_ID=<run_id>`, rollback 때 이전 release의 `release_id`. 비밀값이 아니다. `_replace`의 `same` 판정은 `ddak.spec`(= `config.model_dump_json()` 해시)만 보므로 RELEASE_ID를 spec 해시에 포함한다. 그러지 않으면 같은 digest를 새 run으로 다시 배포할 때 컨테이너를 다시 만들지 않아 `/version.release_id`가 이전 run_id로 남는다(PR #1 cloud health는 `release_id == ctx.run_id`로 대조한다).
  - SECRET_KEY(✅ dev-docs 00 §2 #19·§5-1, 장부 13·32. O3 V1-5가 검사): `keys`에 SECRET_KEY가 있을 때만 코드가 생성·재사용한다. 지금 `inject_config`는 `wanted = … | {"SECRET_KEY"}`라서 v1 부트스트랩(장면 0)이 V1-5에 걸린다. `test_onprem.py` 기대값과 `harness/tests/docker/test_onprem_runtime.py:180`(`inject_config([])` 뒤 `secret_valid` 확인)을 함께 고친다(WP4 범위). 현행(항상 생성)을 유지해야 할 이유가 생기면 `NEEDS_CONTEXT`.
  - SECRET_KEY 외 비밀값(`DATABASE_URL` 등)은 사람이나 provision이 env 파일에 미리 둔다. 없으면 지금처럼 키 이름만 담아 `CONFIG_INVALID`.
  - 한계 명시(docstring·작업 보고): `--env-file` 값은 VM의 `docker inspect`에 보인다(docker 그룹 = root 동등).
- 확인: 허용·거부 표 테스트. 반환값·이벤트·`context.json`에 값이 없음. keys에 SECRET_KEY가 없으면 env 파일에 쓰지 않음. `make -C harness ci`.
- 막히면: O3 env 키 이름이 다르면 O3 기준으로 맞추고 작업 보고에 적는다. C-01 공유 계약을 바꿔야 하면 `NEEDS_CONTEXT`.

### WP5 📌 마이그레이션 VM 경로 — `o1/onprem-vm`, S
- 목적: db VM에 SSH 없이, was VM의 일회성 컨테이너가 MySQL 3306으로 접속해 마이그레이션한다(B5).
- 파일: `onprem/deploy/migrate.py`, 테스트.
- 구현: WP2의 원격 `DockerHost`로 기존 흐름(`provider.py:461-516`)을 그대로 돌린다: `<name>-ddak-migrate`를 create → start → wait → logs 순으로 실행하고(`python -m flaskr.migrate {precheck,up,verify} --json`, `MIGRATE_RESULT` 파싱, strict `_MigrationResult`), finally에서 소유 라벨을 확인한 뒤 제거한다. `--rm`은 쓰지 않는다(wait 직후 지워져 `container logs`가 `ADAPTER_FAILED`가 된다). tier network와 `--env-file`(A안)을 쓰고 Traefik 라벨은 붙이지 않는다(WP3). DB 주소는 env 파일의 `DATABASE_URL`에 있고 코드는 값을 보지 않는다. 컨테이너는 was VM의 `/etc/hosts`를 상속하지 않으므로 env 파일의 DB 주소는 IP로 쓴다(provision에 알리고 작업 보고에 적는다). 시간 제한은 카탈로그 180초와 `ctx.deadline`. 계획 순서(db → app) 때문에 실패하면 app 교체 전에 멈춘다.
- 확인: 가짜 runner로 원격 endpoint와 argv 확인. 러너 출력 이상(추가 필드, ok=false, current≠expected) 거부 테스트 유지. `make -C harness ci`.
- 막히면: 러너 출력 필드 변경은 O3와 C-09 `NEEDS_CONTEXT`. VM 앱 DB·계정 생성 방식은 미결(김준석·정준우).

### WP6 컨테이너 모드 유지 — 모든 PR, S
- 목적: 로컬 검증·테스트 경로를 깨지 않는다(B4).
- 구현: `mode` 생략 = container. 기존 unix 소켓 경로와 포트 규칙(127.0.0.1만)을 그대로 둔다. 필드 규칙: vm 전용 필드는 `ssh` 하나다(container 모드에 오면 거부). `replicas`·`traefik_labels`·`ready`·`public_url`은 두 모드 공통이다. `replicas`를 생략하면 단일 컨테이너 `tier.name`(지금과 같음), 지정하면(1 포함) `<tier.name>-<i>`와 `ddak.replica` 라벨을 쓴다. `replicas`가 2 이상이면 `ports`는 비어 있어야 한다. `ready`가 없으면 container 모드는 지금처럼 준비 확인을 건너뛰고 vm 모드는 기본값으로 한다. vm 모드는 추가로 `ssh` 필수, `ports` 빈 목록, `network`는 `bridge`가 아닌 이름(Traefik 공유 network)이어야 한다.
- 확인: PR마다 `make -C harness ci`. Docker가 있으면 `DDAK_TEST_DOCKER=1 make -C harness test-docker` 결과도 PR에 붙인다(make 타깃은 이 환경변수를 넣지 않는다. 없으면 `harness/tests/docker/test_onprem_runtime.py`가 모두 skip된다).

### WP7 📌 preflight(온프렘 사전 점검) — `o1/preflight`, M
- 목적: 데모·리허설 전에 SSH, Docker, 호스트 키를 확인한다(B6). 10/1 D1 후속은 P1("순서는 정준우 확인")이지만, 데모 직전 점검이라 정준우가 이 계획에서 📌로 올린다.
- 파일: 새 `onprem/deploy/preflight.py`(공개 함수는 `__init__`에 추가), `harness/scripts/o1_demo.py`(`--preflight --inventory <json>`), 예시 `harness/fixtures/o1_demo/inventory.vm.example.json`(주소는 `was.example.invalid` 같은 가짜 값, Notion IP 금지).
- 필수 점검: 입력 모델 검증, 키 파일 stat, env 파일 권한(0600), 호스트 키 지문 일치(jump_host 포함), `ssh … true` 도달(BatchMode), 원격 `docker version`(Server API ≥ 1.49 — `image inspect --platform`이 요구 / OS·Arch와 tier platform 일치), Traefik network 존재, 소유가 아닌 같은 이름 `app-N` 없음, `public_url` 스킴과 Secure 일관성. 선택 점검은 P1로 미룬다.
- 출력: 점검별 `ok|fail|skip`과 redact한 detail을 JSON으로. 실패가 있으면 종료 코드 3. 기존 `preflight()`·`docker_preflight()`는 대체하지 않는다. 시그니처와 `--inventory` 없는 동작(`make preflight`, `tests/e2e/test_o1_fixture.py`가 인자 없는 lambda로 monkeypatch)을 그대로 두고 VM 점검은 새 함수로 분기한다. fixture reset의 `_reset_containers`가 `docker_preflight()`를 쓰므로 `make demo-reset`(`--containers`)은 계속 unix 소켓만 허용한다(그러지 않으면 `DOCKER_HOST=ssh://`일 때 VM의 컨테이너를 `rm -f`할 수 있다).
- ops 툴 등록(`ops/tools/preflight_check/tool.py`)은 **보류**한다. ops는 import-linter 계약 3 때문에 `onprem.deploy`·`cd`를 import할 수 없고, 입출력 스키마도 새로 생긴다. 선택지를 `NEEDS_CONTEXT`로 올린다: (가) `ddak.ops.tools.* -> ddak.onprem.deploy` 예외 추가(pyproject 공유 파일, PR #1과 충돌 주의), (나) CdProvider에 점검 함수 추가(C-21, C2 영향), (다) 대회 중에는 스크립트로만 운용. 기본안은 (다).
- 확인: 점검별 성공·실패 가짜 runner 테스트, 기존 `make preflight`·`make demo-reset` 동작 유지, `make -C harness ci`. VM 준비 알림 뒤 실제 VM에서 1회.

### WP8 📌 reset_demo_state(온프렘 몫) — `o1/demo-reset`, M
- 목적: 리허설 사이에 온프렘을 v1 운영 상태로 되돌린다(10/1 D4, roles/O1 §4-2).
- 파일: 새 `onprem/deploy/demo.py`(공개 함수), `harness/scripts/o1_demo.py`(`--reset --mode real --state <dir> --project <name> --inventory <json> --release <run_id>`). `core/store.py`는 고치지 않는다(`Store(<state>/ddak.sqlite).connection()`을 쓴다).
- 순서:
  1. 가드: `ALLOW_DEMO_RESET=1`. `--project`가 스크립트 상수 허용 목록(기본 flaskr)에 있음. `--state`(기본: 앱 상태 루트 = `DDAK_RUN_DIR`의 부모, 보통 `harness/var`)의 `controller.lock` flock 획득(앱이 떠 있으면 거부. 리허설 전 `make run`을 멈춘다). `locks` 행 없음. `runs`·`env_release` 어디든 RUNNING·NEEDS_HUMAN이 있으면 거부하고 사람이 확인한다(기존 `reset_guard`와 같고, O1.md "일반 운영 잠금 강제 해제·임의 DB 초기화 명령은 제공하지 않는다"). DIVERGED는 잠금을 남기지 않으므로 가드를 통과한다.
  2. 기준 release 검증: `releases`에 있는 `source_mode=real` 기록, 같은 프로젝트, `images[tier]` 있음.
  3. env 파일에서 `SECRET_KEY` 줄만 삭제(다른 키 보존, 0600 유지). 4보다 먼저 해야 새로 만드는 v1 replica가 `--env-file`로 SECRET_KEY를 받지 않는다(V1-5).
  4. replica 복구: `OnPremProvider.rollback(tier, ctx)`를 `previous_release={"local": 기준 release}`인 합성 컨텍스트로 불러 WP3 절차를 재사용한다. 실패 주입 뒤처럼 이미 v1 digest인 replica도 SECRET_KEY를 가진 채 남아 있으면 다시 만든다(env 파일 키 **이름** 해시를 `ddak.envkeys`에 분리하고 reset에서만 비교한다. 실행기 rollback의 `ddak.spec`에는 넣지 않는다).
  5. 장부(📌, 빠지면 다음 v2 run의 롤백 대상과 O2 변경 탐지 기준이 rel-v2로 남아 반복이 깨진다): 트랜잭션 1개로 `env_release.local`을 current=기준 manifest의 release 키(`release_id`·`source_mode`·`source`·`files`·`source_files`·`images`·`artifacts`)를 `release_view`에 통과시킨 값(`service.finish`와 같은 모양), previous=이전 current, status=SUCCEEDED로 바꾼다. 데모 프로젝트의 DIVERGED는 해제한다(`acquire`가 프로젝트 단위로 막는다). cloud 행은 DIVERGED인 경우에만 status를 ROLLED_BACK으로 바꾸고 current는 그대로 둔다(PARITY_FAILED 때 클라우드는 이미 롤백됐다. 클라우드 release 되돌림은 C2 몫, §6). 감사 JSON(전후 값, 복구 replica)을 `var/ops/`에 남긴다. PR #1 merge 뒤 Store 메서드로 옮길지는 P1.
  6. DB 0001(P1): v1은 0002 스키마에서도 동작하므로(dev-docs 01 §11-2 #2) 반복에는 필요 없다. "0002 이미 적용 — 다음 v2 run의 prepare_db는 applied=[]" 경고만 출력한다.
- 금지: 일반 프로젝트 대상, 잠금 강제 해제(NEEDS_HUMAN 포함), 소유 라벨 없는 컨테이너 삭제, 클라우드 조작(안승환 몫. C2 provider가 준비되면 같은 스크립트에서 부를 자리만 둔다).
- ops 툴(`ops/tools/reset_demo_state/tool.py`) 등록은 WP7과 같은 이유로 `NEEDS_CONTEXT`(기본안: 스크립트 운용).
- 확인: 가드별 거부(NEEDS_HUMAN·잠금·RUNNING·허용 목록 밖·flock), SECRET_KEY 삭제가 replica 복구보다 먼저, replica 복구, 장부 갱신과 DIVERGED 해제, 연속 2회 멱등을 가짜 runner로. 기존 fixture `make -C harness demo-reset` 동작 유지. `make -C harness ci`.

### WP9 📌 리허설: v1 부트스트랩 → v2 ×3 → 실패 주입 — `o1/rehearsal`, M
- 목적: DeploymentService 경유로 장면 0–2를 반복해 E2E 기준(부트스트랩 1 + 업데이트 3회 연속 + 실패 주입 1)을 O1 쪽에서 증명한다.
- 파일: `harness/tests/docker/service_rehearsal.py`·`harness/tests/docker/fixture` 확장, 새 `harness/tests/docker/test_onprem_vm.py`(opt-in).
- 구현:
  - 컨테이너 모드: fixture 이미지로 bootstrap → v2 update ×3 → 실패 주입 → reset → 재실행.
  - VM 모드: `DDAK_TEST_VM=1`, `DDAK_TEST_VM_INVENTORY=<json>`, `DDAK_TEST_VM_IMAGES=<json>`(정준우가 올린 멀티 아키텍처 digest), `DDAK_TEST_VM_URL`(Traefik 진입점)이 있을 때만 실행. `ports`가 없으므로 HTTP 확인은 이 URL로 하고, Traefik이 replica를 번갈아 고르므로 같은 버전이 연속 N회 나올 때까지 폴링한다. 정준우가 올릴 이미지는 fixture v1·v2·broken 3개다. 기존 `docker` 마커를 쓰고 pyproject는 고치지 않는다.
  - 실패 주입(제품 코드 플래그 없음): fixture 앱은 모든 GET에 200을 주므로 계획에서 `deploy.db.local`을 빼도 실패하지 않는다. (1) 준비 실패(기본): fixture Dockerfile·server.py에 `READY` build-arg를 더해 `READY=fail` 이미지는 `/health/ready`에 503을 준다(테스트 코드만 수정). v2 대신 이 broken 이미지로 update → replica 1 준비 실패 → `deploy_tier` `ADAPTER_FAILED` → rollback. (2) health 실패: 기존 `service_rehearsal.py` 테스트 전용 health의 `state["force_failure"]`(O1.md "강제 헬스 실패")를 유지한다. dev-docs 00 §9-3 #3·roles/O1 §4-1의 "리허설 전용 플래그(migrate 건너뛰기)"를 제품 코드 플래그로 만들지 않는 이유: AGENTS §3이 로컬 검증을 건너뛰는 플래그를 금지하고, 제품 경로에 실패 분기를 두지 않기 위해서다. flaskr에서 `deploy.db.local`을 빼는 주입은 v2 `/health/ready`가 스키마 버전을 검사할 때만 성립하므로 WP12에서 O3에 확인한다.
  - health/smoke는 테스트 전용 HTTP 검사이고 결과에 `source=rehearsal` 라벨을 붙인다(불변 조건 h). 제품 레지스트리에는 등록하지 않는다.
- 확인: 컨테이너 모드 3회 연속 성공과 실패 주입 1회의 소요 초를 `var/validation/`에 남기고 작업 보고에 요약(O1.md에도 반영). VM 모드는 VM 준비 알림 뒤.
- 막히면: VM·이미지 미준비 → VM 부분만 `BLOCKED`. 정준우가 준비 완료를 알리기 전에는 실제 SSH 접속을 시도하지 않는다.

### WP10 (P1) 승인 화면 메타 전달 — 서비스 구현 DONE (10/1 후속 지시)
- 목적: 승인 화면에 패치 이유·재사용/출처와 인프라 plan 요약(C-18)을 보일 수 있게 한다(PR #1 리뷰, 10/1 C).
- 파일: `executor/service.py`, 새 테스트 파일.
- 결정: 사용자가 `patch_meta={reason,reuse,source}`와 C-18 임시 요약 구조 구현을 승인했다. 공유 스키마·C3 웹·C1 코드 수정 없음. 상세 필드는 [O1.md](O1.md)의 WP10 절.
- 구현: `prepare(..., patch_meta=None, infra_summary=None)`. 각각 UTF-8 JSON 8KiB 이하, 모양·redact 검사 후 불변 문자열과 `approval-meta.json`으로 저장, 조회에는 복사본 반환. 인프라 요약 누락/다른 plan sha256이면 승인·시작 거부, 거절은 허용. 저장 파일 변경도 승인/실행 전에 차단한다. `reuse`는 표시용이며 재승인 면제가 아니다.
- 확인: 기존 호출 호환, 대상별 한 번 승인, 메타 변조·누락·해시 불일치·크기/민감값/형식 오류 검사. `make -C harness ci`: WP10 시점 504 passed, 2 deselected.

### WP11 PR #1 merge 뒤 웹 연결 — `o1/project-settings`, M
- 선행: PR #1 merge(사람만). merge 전에는 PR #1 diff와 겹치는 `core/store.py`, `app.py`, `cd/__init__.py`, `cd/interface.py`, `core/contracts/tools/*` 전체, `pyproject.toml`, `test_store.py`, `test_cd_interface.py`를 고치지 않는다.
- D6 공개 조회: `DeploymentService`에 `run`, `list_runs`, `approvals`, `environments`, `project_settings`, `save_project_settings(project, data, *, updated_by, expected_version)` 래퍼. 재생과 구독 사이 누락을 막는 원자적 `follow(run_id, after, sub)`도 만든다. `web/`은 고치지 않고 양서윤에게 교체를 알린다.
- D5(P1, B10 ⏸ 동안): `prepare`에서 `store.project_settings(project)`의 `cloud_domain`을 `RunContext.cloud_domain`으로 스냅샷한다(`dataclasses.replace`). 호출자가 다른 값을 넘기면 `CONFIG_INVALID`. dns_mode·hosted_zone_id는 RunContext 필드가 없으므로 `NEEDS_CONTEXT`(C-07).
- D7: `project_settings_history` 테이블과 `expected_version` 필수안을 선택지·영향과 함께 `NEEDS_CONTEXT`(C3). 결정 전에는 구현하지 않는다.
- `app.py` 연결(공유 파일, 최소 줄): refresh와 platform 로더 주입 자리. RunContext 조립 위치와 계획 흐름 조정자는 §6 둘째 행의 결정을 따른다.
- 확인: 새 테스트 파일(`test_store.py` 끝에 붙이지 않는다), `make -C harness ci`.

### WP12 flaskr + MySQL 실검증 — `o1/real-validation`, M
- 선행: O3의 flaskr v1/v2·Dockerfile·`flaskr.migrate` 러너, 멀티 아키텍처 이미지 digest(C2 또는 정준우).
- 컨테이너 모드: `harness/compose/local`(O2)의 MySQL로 inject → migrate 0001/0002 → deploy → health/smoke(O3 툴 등록 시)를 돌리고, 실제 러너 출력이 `_MigrationResult`와 맞는지 확인한다. `compose/local/.secrets/`(MySQL 비밀번호)와 env 파일의 `DATABASE_URL` 값은 사람이 준비한다. Codex는 `.secrets/`를 만들거나 읽지 않고, 비밀번호를 생성하거나 쓰지 않는다(§3, AGENTS §3). 준비되지 않았으면 `BLOCKED`로 보고한다.
- VM 모드: VM 준비 알림 뒤 WP7 preflight → WP9 VM 리허설을 실제 이미지로.
- flaskr 실패 주입: v2 `/health/ready`가 스키마 버전을 검사하는지 O3에 확인하고, 검사하면 `deploy.db.local`을 뺀 계획으로 장면 2를 재현한다.
- 막히면: 산출물이 없으면 `BLOCKED: O3 sample-app 미제공`처럼 보고한다. `apps/sample-app`을 직접 쓰지 않는다.

### WP13 문서 — 각 PR과 마지막, S
- [O1 가이드](O1.md)의 바뀐 부분(인벤토리 필드 표, SSH 방식, replica·롤백, env 파일 한계, preflight·reset 사용법, 검증 결과)을 고친다. 그 전에는 작업 보고 "문서 반영 대기" 절에 적는다(§4). `harness/docs/ai-usage/O1.md` append는 지금 해도 된다.
- 10/1 기록의 "남은 문서 정리" 목록과 dev-docs·roles의 낡은 문구는 고치지 않고 보고에 목록으로만 적는다.

**P1(시간이 남으면)**: container 모드 `docker_host`의 `ssh://` 제한(10/1 사용자 결정으로 현재 허용 유지). **알려진 위험: container 모드 ssh://는 지문 고정 없이 사용자 ssh 설정을 쓴다. 데모는 vm 모드 인벤토리 사용.** 그 밖에 WP10 화면 연결(서비스 완료), WP7 선택 점검(env 파일 필수 키 이름 `--keys a,b`, 노트북 `buildx imagetools inspect` digest 조회 `--images <json>`, 원격 pull `--pull`일 때만), WP8 DB 0001 복원, WP8 장부 갱신을 Store 메서드로 이동(PR #1 merge 뒤), WP11 D5(B10 ⏸ 동안), SSH 전경 master(SSH 왕복이 tier당 30초를 넘을 때: provider 소유 `ssh -M -N`(stdio=DEVNULL, finally 종료) + 클라이언트 `ControlMaster no`), 롤백 때 VM에 같은 digest 이미지가 있으면 pull 생략, `collect_diagnostics`(local), step별 타임아웃(김준석과), VM env 파일 B안, VM 전 `ssh://` 경로 로컬 검증(opt-in `DDAK_TEST_SSH=1`, docker:dind + sshd, 테스트 중 만든 일회용 키. 💭 정준우 확인). 온프렘 pre-pull은 `deploy_tier`의 pull로 대신하고 `build_image` local 어댑터는 만들지 않는다(💭, 미결 표).

**일정(💭)**: 10/1 밤 WP1 → WP2. 같은 때 §6 첫 두 행의 `NEEDS_CONTEXT`를 바로 올린다. · 10/2 오전 WP3 → 바로 컨테이너 모드 실 Docker 확인(replicas 3, ports [], 사용자 정의 network, 준비 확인, broken 이미지 롤백. `test_onprem_runtime.py` opt-in 확장) → WP4·WP5. · 10/2 낮 WP8 → WP7(필수) → WP9 컨테이너 모드 3회 + 실패 주입. · VM 준비 알림이 오면 진행 중 WP를 보고로 정리하고 WP7 실 VM 1회 → WP9 VM(fixture 이미지) 1회를 먼저 한다(WP12는 O3 산출물 뒤). · PR #1 merge 뒤 WP11(D6·follow). · 남는 시간 P1. · WP13은 WP마다 바로.

## 3. 건드리지 않을 것

- 다른 담당 디렉토리: `plan/**`, `verify/**`, `cloud/**`(build·deploy·health·infra·tls 모두), `onprem/provision`·`onprem/inventory`(김준석), `web/**`, `apps/sample-app`, `harness/compose/local`, `cd/tools/health_check`(PR #1).
- C1 일(`cloud/infra`, `cloud/tls`, refresh 구현, plan 요약): 정준우가 별도로 지시하기 전에는 착수하지 않는다.
- PR #1(`c3`)과 `c2` 브랜치: push, merge, 리뷰 게시를 하지 않는다. `.worktrees/pr-1`은 건드리지 않는다.
- 툴 이름·파라미터·입출력 스키마·이벤트·카탈로그 메타정보·deploy.yaml 키·ErrorCode·RunContext 필드·import-linter 계약 변경: `NEEDS_CONTEXT`.
- 공유 파일(`pyproject.toml`, `uv.lock`, `.github/`, `.githooks/`, `.claude/`, `AGENTS.md`, `harness/Makefile`, `harness/scripts/dev.py`)과 새 의존성. SSH는 시스템 `ssh` CLI로 하고 paramiko 등을 넣지 않는다.
- 실제 VM·SSH 접속(정준우 준비 알림 전), web·db VM 조작, Traefik·cloudflared 구성, `docker login` 자동화, 토큰 값 취급, Docker Hub push.
- `~/.ssh`, `~/.aws`, `.env*`, `.secrets/`, tfstate 읽기와 `.secrets/` 생성. commit·push·PR 생성(정준우가 한다), 로컬 미커밋 변경을 stash·reset·restore로 버리는 것.

## 4. 작업 방식

- **git (✅ 10/1 정준우):** Codex는 메인 작업 트리의 **로컬 파일을 기준**으로 작업한다(아직 커밋 안 된 문서 포함). git 이력은 참고만 한다. **브랜치는 만들어도 된다**(`git switch -c o1/<주제>`, 미커밋 변경을 그대로 가지고 간다). **commit·push·PR 생성은 하지 않는다. 정준우가 한다(10/1 결정).** 로컬 변경을 stash·reset·restore·checkout으로 버리지 않고, 별도 worktree를 쓰지 않는다(미커밋 문서가 안 보인다).
- WP가 끝나면 보고(`DONE`)에 바뀐 파일 목록, 실행한 명령과 결과, **제안 커밋 메시지**(한국어, AI attribution 없음)를 적는다. 정준우가 확인 후 커밋·push·PR을 한다.
- 문서(`O1.md`, 이 계획, 10/1 기록)는 로컬에서 바로 고친다. 정준우가 함께 커밋한다. `harness/docs/ai-usage/O1.md` append도 바로 한다.
- 독립 검토는 30분 점검 때 오케스트레이터가 한다. 검토를 기다리며 멈추지 않고 다음 WP를 계속한다. 지적이 오면 반영한다.
- 오케스트레이터도 같은 작업 트리의 문서를 고칠 수 있다. 파일을 고치기 전에 다시 읽는다.
- TDD가 아니다. 구현한 뒤 핵심 경로 테스트(위 각 WP의 "확인")를 붙인다.
- 제안 커밋 메시지·보고에 AI attribution을 넣지 않는다. AI 사용 기록은 `docs/ai-usage/O1.md`에만 둔다.
- 끝낼 때 `make -C harness ci`를 직접 돌려 출력으로 확인한다. 원격 CI는 정준우가 push한 뒤 확인한다. 보고는 `DONE`/`NEEDS_CONTEXT`/`BLOCKED`.
- 오케스트레이터가 30분마다 점검한다. 점검 때 3줄로 답한다: 진행 중 WP와 브랜치·바뀐 파일, 다음 할 일, 막힌 점(`NEEDS_CONTEXT` 대기 포함).
- `NEEDS_CONTEXT`를 올려도 그 항목만 멈추고 다른 WP는 계속한다.

## 5. 다른 담당과의 연결점

| 담당 | O1이 주는 것 | O1이 받는 것 | 지금 대체 | 시점·주의 |
|---|---|---|---|---|
| O2 김준석 | `prepare(plan, ctx, source, patch, subjects, facts_reader)`, `Store.environments()`(마지막 성공 release의 원본 파일 해시 = detect 기준), provider 인벤토리 필드 표(WP2) | 검증된 `Plan`, `facts_reader`, `load_inventory(path) -> dict`(provider 모양, JSON 기본형만), `public_url`(provision), Traefik·network·deploy 사용자·known_hosts 준비. provision은 app-N을 띄우지 않는다. was VM 요구: Docker Engine ≥ 28.1(API 1.49, `image inspect --platform`. Ubuntu `docker.io` 패키지는 더 낮을 수 있어 docker-ce 저장소. 노트북 CLI도 ≥ 28.1), 원격 docker CLI(`docker system dial-stdio`), Traefik 라우터 규칙은 `Host` 대신 ``PathPrefix(`/`)``(💭 임시 터널 주소가 재시작마다 바뀜), replica 전체에 같은 라우터·서비스 이름을 명시한 라벨 세트 | 테스트용 provider 모양 dict, `source_facts`, `golden_v2_update.json` | WP2 보고를 정준우가 김준석(C-04)에게 전달. 계획 흐름 조정자 미정(§6) |
| O3 장민영 | `OnPremProvider.health_check` 위임 자리(`onprem/deploy/health.py`), replica 준비 확인, env 키 주입(`public_env`, `RELEASE_ID`) | flaskr v1/v2·러너(`MIGRATE_RESULT`, 추가 필드 금지), `passed: bool` 있는 health/smoke 등록, 준비 경로·포트, env 키 이름, 패치 메타 | fixture 이미지·가짜 러너(`harness/tests/docker/fixture`), `health_checker` fake | 실검증(WP12)은 O3 산출물 뒤. sample-app README의 "Secure 클라우드만"은 O3가 고칠 일. 온프렘 Secure 쿠키를 켜면 http(Traefik 직접)로 하는 로그인 smoke는 쿠키가 실리지 않아 실패한다. local smoke는 `ctx.platform["onprem"]["public_url"]`(HTTPS)로 하거나 ProxyFix가 믿는 `X-Forwarded-Proto: https`를 보내야 한다 |
| C2 안승환 | `ctx.build_source`·`images`·`previous_release['cloud']`·`lock_token`, `CdProvider`·`ProviderResult` | `release_artifacts`(index + platform_digests), cloud `deploy_tier`의 `observation`, 클라우드 reset 몫 | `FakeProvider`, 테스트용 빌드 툴 | `select_provider`가 adapter_mode 하나로 고르므로 VM 실검증은 local만 있는 계획으로 한다 |
| C3 양서윤 | `approval_view`·`approve`·`start`·`events`·`subscribe`·`shutdown`, (WP11) 공개 조회·`follow`, 결과 dict 모양 유지 | 웹이 `service.store`를 직접 쓰지 않도록 교체, 승인 화면에 새 키 표시 | PR #1 현재 코드 | WP11은 PR #1 merge 뒤. 종료 상태를 더하면 app.js 목록도 고쳐야 한다 |
| C1(정준우 본인 몫, 별도) | 승인 대상 `infra` 해시, refresh 주입 자리 | `refresh(step, out, ctx)`, C-18 plan 요약 | `refresh=lambda s, o, c: c`는 테스트에서만 쓴다. `app.py`나 제품 경로에 넣으면 `apply_infra` 앞의 `INFRA_MISSING` 차단이 사라진다 | 지시 전 착수 금지 |

## 6. 미결 사항 (Codex는 기본안으로 진행하고 작업 보고에 적는다)

| 결정 | 막는 WP | 기본안 | 결정자 |
|---|---|---|---|
| 로컬 health_check 구현 | 장면 0–2 | ✅ 10/1 사용자 확정: O1 health.py 구현, O3 smoke 유지. 카탈로그 owners 변경 없음. WP3 provider 구현 완료, 등록 툴 연동은 PR #1 merge 뒤 | 정준우 |
| RunContext 조립·계획 흐름 조정자 | 장면 0·1, WP11 | ✅ 10/1 사용자 확정: PR #1 merge 후 ddak.app에서 intake→detect→plan→validate→prepare 및 인벤토리 주입 조립. merge 전 app.py 변경 금지 | 정준우 |
| 인벤토리 VM 필드 이름·모양(C-04), `hosts`/`addresses`와 `docker_host`/`tiers` 통일 | WP2 | provider 모양 + 선택 필드 | 정준우·김준석 |
| SSH 설정 전달 방식 | WP2 | 래퍼 ssh(vm argv의 PATH 앞) | Codex가 PR에 근거 |
| Traefik 라벨 키, network 이름, replica 수, 준비 경로·포트 | WP3 | 인벤토리 값, 경로 `/health/ready` | 김준석·장민영 |
| env 파일 위치(노트북 A / was VM B) | WP4·WP5 | A | 정준우(+O3) |
| ProxyFix hop 수, 외부 공개 방식(B7 ⏸ 미정) | WP4 | 값으로만 전달 | 정준우·장민영·김준석 |
| Docker Hub pull 인증 위치 | WP7·WP12 | 노트북 자격, 리허설에서 확인 | 정준우·김준석 |
| ops 툴 등록 방식(import 예외 / CdProvider / 스크립트)과 입출력 스키마 | WP7·WP8 | ✅ 10/1 사용자 확정: 일단 운영 스크립트로 제공. 코드 취합 때 ops 등록 여부 재검토 | 정준우(TL 대행) |
| reset의 cloud 행 DIVERGED 해제(status만 ROLLED_BACK, current 유지) | WP8 | 기본안대로 하고 작업 보고에 적는다 | 정준우·안승환 |
| reset의 DB 0001 복원 방식 | WP8(P1) | 건너뛰고 경고 | 정준우·장민영·김준석 |
| `approval_view` 새 키 이름·모양 | WP10(P1) | ✅ 10/1 사용자: 코드 취합 때 진행. 현재 구현 보류. 질문 목록은 O1.md | 정준우·장민영·양서윤 |
| `project_settings` 이력·`expected_version` 필수(D7), dns_mode 필드 | WP11 | 결정 전 미구현 | 정준우·양서윤 |
| `snapshot`·`step_results`·승인 대상 필드(C-07) | WP11·팀 통합 | `NEEDS_CONTEXT` | 정준우·김준석·양서윤 |
| 교차 검증 불일치 정책 | - | 클라우드만 롤백(현 구현) | 팀 |
| 온프렘 pre-pull 위치(`build_image` local) | - | `deploy_tier` 안 pull | 정준우·김준석 |
| Codex의 commit·push·PR | 전체 | ✅ 하지 않음(정준우가 함, 브랜치 생성은 허용) | 정준우(10/1 결정) |
