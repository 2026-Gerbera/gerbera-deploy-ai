# C1 인수인계: AI Terraform 생성(generate_infra) — 김준석

> 표기: ✅ 확정 / 🟡 합의 대기 / 💭 고려안 / ⏸ 보류. 기준일 2026-10-01. 원문 [roles/C1](../roles/C1_클라우드인프라-AI테라폼-10월1일%20수정본.md)(462줄)에서 `generate_infra`에 필요한 부분만 뽑았습니다. 이 문서와 원문·[공통 계약](../01_공통-계약.md)이 다르면 원문 쪽을 따르고 정준우에게 알려 주세요.

> **요약**
> - **무엇을:** `cloud/infra/tools/generate_infra` 하나를 만듭니다. 인프라 요구(C-17)와 환경 정보 가린 요약을 받아 Claude(`call_ai`)로 Terraform HCL 초안(리소스 블록만)과 메타를 만드는 AI 툴입니다. 검사·plan·승인·apply는 정준우가 맡습니다.
> - **왜 준석님이:** 유상준(C1)이 10/1에 하차했습니다. 준석님이 `call_ai`·Jev·분석·계획을 이미 맡고 있어서 analyze → planner → generate_infra가 한 라인이 되고, 분석의 비밀값 키 분류가 곧 시크릿 리소스 입력이 됩니다(분담 ✅ / 세부 🟡).
> - **언제까지:** 앱 층 수정안을 먼저 만들고 💭 10/2까지 끝내는 것이 목표입니다. 예선은 10/3–10/4입니다(10/3 10:00 제출, 10/4 결선 라이브).
> - **무엇부터:** ① 1절 🟡 합의 → ② 입출력 모델 → ③ v2 앱 층을 fake·replay로(cli live 1회) → ④ 정준우 C-20·검사 툴과 맞춤. fake·replay 개발은 먼저 해도 되지만, live plan "+1 ~1 -0" 확인 전에 **플랫폼 층 + 앱 v1 apply가 먼저**입니다(C1 §9-1: 10/1 오후 부트스트랩 → 10/1 저녁 v2 반복).
> - **오늘 혼자 할 수 있는 것:** 입출력 모델 초안, fake 어댑터, FakeProvider 단위 테스트, prompt.md 초안. v2 "+1 ~1 -0"까지 가려면 1절 0·2·7번과 C-20 상수를 정준우와 먼저 정해야 합니다.

## 1. 분담

| 영역 | 김준석 | 정준우 |
|---|---|---|
| AI 초안 | `generate_infra`: 앱 층(시크릿 리소스 + 실행 역할 읽기 권한), 플랫폼 층(준비 단계 1회) | – |
| 고정 틀 | 읽기만(이름 규칙 변수·허용 목록·경계 변수, C-20) | state 버킷 + 권한 경계, 코드 소유 틀(버전·provider·backend·층별 state 키), 리소스 허용 목록 |
| 검사 | – | `validate_infra`: fmt/validate + 정책 검사(경계 부착, 와일드카드 관리자 권한 금지, destroy/replace 차단, 허용 리소스 목록) |
| plan·승인·apply | – | `plan_infra`(plan -json → C-18 요약), `apply_infra`(승인된 plan 해시만), `refresh`(`terraform output -json` → RunContext) |
| TLS | – | `cloud/tls`(`ensure_tls`) |
| 입력 쪽(이미 O2 몫) | C-17 `infra_needs`·`infra_inputs_hash`, C-22 가린 요약 변환, C-12 `call_ai`, `validate_plan` V22~V24 | C-22 `RunContext` 조립, Terraform 출력 허용 목록 |

**🟡 확인 부탁 (정준우와 먼저 합의)**
0. **개선 배포에서 기존 앱 층을 어떻게 보존할지(가장 먼저):** "+1 ~1 -0"이 되려면 기존 리소스의 주소(`type.name`)·속성이 그대로여야 합니다(아니면 replace·delete → V23 BLOCK). 그런데 C1 §5-1은 "생성물을 AI 입력으로 되돌리지 않음"이고, 원문을 data로 넣어도 redact가 `secret_id = …`·`manage_master_user_password = …` 값을 가리고 4096자에서 자릅니다(2절 함정).
   - (A) 마지막 승인 앱 층 HCL을 data로: 단순하지만 위 두 문제 + §5-1 충돌.
   - (B, 💭 권고) AI는 바뀐 블록만 냄(새 시크릿 1 + 같은 주소의 시크릿 읽기 정책 1). 코드가 마지막 승인 번들에서 같은 주소를 교체·추가. AI 입력은 "기존 리소스 주소 목록 + 정책이 허용 중인 시크릿 키 이름"만. 리소스당 파일 하나(`<type>.<name>.tf`)면 병합은 파일 교체.
   - (C) v1을 시크릿 이름 목록 변수(`for_each`)로: v2는 변수만 바뀌어 AI 호출이 없어지므로 데모 서사(✅ 장부 27·32 "AI가 수정안 제안")와 충돌.
   - 어느 쪽이든 마지막 apply된 층별 번들 해시를 실행기 DB에 남겨야 합니다(정준우 `apply_infra`·`record_deploy_log`).
1. 분담 수락 여부. 세부 나눔은 준석님이 확인하기 전까지 🟡입니다.
2. **저장 위치와 해시 3종:** 분담 계약은 `var/infra/<run_id>/`, 공통 계약 12절·N34는 `var/infra/<입력 해시>/`, C1 §4-1 `validate_infra` 입력은 "HCL 묶음 해시"로 서로 다릅니다. 해시를 나눠 부릅니다: `infra_inputs_hash`(C-17, V22 트리거, 요구만) / `generation_key`(캐시 키: 요구 + 층 + C-20·provider·prompt 버전 + 이전 승인 번들 해시) / `bundle_sha256`(AI 파일 내용 해시).
   - 💭 선택지 예: 파일은 `var/infra/<bundle_sha256>/`(내용 주소라 `validate_infra`가 해시로 찾음), run 기록은 `var/runs/<run_id>/infra.json`에 두 해시만. 기준 디렉토리는 `Settings.run_dir.parent / "infra"`(상대 경로, `make` cwd인 `harness/` 기준).
3. **수정 루프(최대 3회) 조정자를 누가 돌릴지.** C1 §10-1이 준석님과 정할 항목으로 남겼습니다. 코드 사실: 실행기는 층이 `outside`인 툴을 계획에서 실행하지 않습니다(`engine.py` `_check_tool`, `service.prepare`가 `PLAN_INVALID`). 그래서 analyze → generate_plan → generate_infra ⇄ validate_infra → plan_infra → validate_plan → `prepare(..., subjects={"infra": plan_sha256})`를 잇는 조정자가 따로 필요합니다(roles/O2 §4-1 "재지시 조정자" 자리 💭).
   - 툴은 `REGISTRY.get(name).fn(inp, ctx)`로만 부르고 매번 `with runtime.tool_context("generate_infra", run_id):`로 감쌉니다(안 감싸면 `AI_NOT_ALLOWED`). 카탈로그 `timeout_s` 강제도 조정자 몫입니다.
   - web·executor·core는 `ddak.plan`을 import할 수 없으므로(계약 4) 조정자는 `ddak.app`이 주입합니다(`facts_reader`와 같은 방식). 권고 💭: 루프는 준석님, `validate_infra`·`plan_infra`는 정준우가 레지스트리 툴로 제공.
4. **입출력 모델:** `src/ddak/core/contracts/tools/generate_infra.py`에 `GenerateInfraInput(ToolInput)`·`GenerateInfraOutput(ContractModel)`(`export_schemas.py`가 `<Pascal>Input/Output` 이름으로만 찾음). 새 스키마라 합의 뒤 `make -C harness contracts-update`로 넣습니다. `target`은 `Target.CLOUD`만.
   - `InfraNeeds`(C-17)·가린 요약 모델(C-22)과 요약 **변환 함수**는 `core/contracts/`·`core` 쪽 공용 위치(💭 예: `core/contracts/infra.py`)에 둡니다. plan·cloud 둘 다 쓰는데 툴 디렉토리끼리는 import 금지(계약 3)라서, roles/O2 §4-1의 "core 또는 plan"은 core로 정합니다(또는 조정자가 계산해 입력으로 넘김).
   - 💭 `infra_needs`·`env_summary`는 입력 필드로 받습니다(`RunContext`(C-07) 필드 추가는 계약 변경이 하나 더 생김). `mode`는 이미 `ctx.mode`. `layer: Literal["platform","app"]`를 두고 부트스트랩은 층마다 1회(해시·캐시·루프 따로), 개선 배포는 `app`만.
5. **범위 축소안 💭:** 기존 리소스 탐지 ⏸, cleanup 툴 제외(사람이 `terraform destroy`), 플랫폼 Terraform은 준비 단계 1회, ACM·443·리다이렉트를 플랫폼 Terraform에 포함. 마지막 안을 채택하면 6절 ALB 규칙("80 리스너까지만")이 바뀌고, 인증서 도메인은 코드 변수 참조만 허용해야 합니다(도메인은 AI 입력·출력 금지 ✅ 장부 17-a, 클라우드 도메인 결정은 ⏸ B10).
6. **플랫폼 층 생성 전에 정할 것:** N42(ECS 서비스·대상 그룹을 앱 층에 둘지), D3(web·was 한 태스크 2컨테이너), Fargate 기본 아키텍처(💭 ARM64 비용 유리, 안승환과). 앱 역할 4개와 실행 역할 시크릿 읽기 정책의 층도 정합니다. v2 "IAM 정책 1 변경"은 그 정책이 앱 층에 있어야 앱 층 plan에 나옵니다.
7. **`variable`·`output`·`locals`는 누가 쓰나:** 코드 틀과 AI가 둘 다 선언하면 `terraform validate`가 중복으로 실패합니다. v2 새 시크릿 ARN이 `terraform output -json`에 없으면 `apply_infra`가 기록하지 못하고 `inject_env_config`(안승환)가 valueFrom을 못 채웁니다. 권고 💭: `variable` 선언은 코드 틀(C-20)만(AI 파일에 있으면 G-1 거부), `output`은 AI가 이름 규칙(예: `app_secret_arn_<KEY>`)으로 쓰고 `refresh` 허용 목록이 그 패턴을 받음, 리소스 주소도 이름 규칙으로 고정.
8. **권한 경계 적용 범위:** 분담 계약은 "모든 `aws_iam_role`에 경계 변수"입니다. C1 §5-2 G-1은 `/ddak/app/` 역할의 경계 누락만 검사하고 P0 경계는 앱용 `ddak-app-boundary` 하나뿐입니다(파이프라인 경계 P1, N30). 플랫폼 역할(`ddak-deployer`·`ddak-codebuild`)에 붙일 경계 변수를 정준우와 확인합니다.

## 2. 만들 것

```
src/ddak/cloud/infra/tools/generate_infra/
├── __init__.py   비워 둠(자동 등록·import-linter 와일드카드 때문)
├── tool.py       @tool("generate_infra") + 어댑터 선택만. 로직 없음
├── logic.py      입력 → 프롬프트 데이터 조립, 출력 후처리(파일·메타 저장)
├── cloud.py      call_ai 호출 경로. adapter_mode가 FAKE가 아니면 여기로 옴(backend cli/api/replay는 설정 DDAK_LLM_BACKEND가 고름, 기본 replay). local.py 불필요(AdapterSet local=None)
├── fake.py       결정적 정상/실패 시나리오(테스트·UI 개발, source=fixture)
└── prompt.md     머리말에 버전·담당·변경일. 6절 규칙 문구
src/ddak/core/contracts/tools/generate_infra.py      GenerateInfraInput / GenerateInfraOutput (🟡 합의 후)
harness/tests/unit/cloud/infra/tools/generate_infra/  테스트
harness/fixtures/ai_replay/generate_infra/<키>.json   replay 저장 응답(source=replay)
```

- **등록:** [`core/tools/ping/`](../../../src/ddak/core/tools/ping/tool.py)을 복사해서 시작합니다. 시그니처는 `def generate_infra(inp: GenerateInfraInput, ctx: RunContext) -> GenerateInfraOutput`이고, 어댑터는 `select_adapter(ADAPTERS, inp.target, ctx.deploy_config, ctx.adapter_mode)`로 고릅니다. 이름·모듈·시그니처가 카탈로그와 다르면 앱 기동이 실패합니다. LangChain·MCP SDK는 쓰지 않습니다.
- **카탈로그 메타**(이미 있음, `registry.py` `_ROWS` / `tool_catalog.json`): 모듈 `cloud.infra`, 단계 `plan`(①), 층 `outside`(계획 밖), 대상 `[cloud]`, `uses_ai=true`, `read_only=true`, 잠금·승인 없음, `timeout_s=300`(AI 1회분이고, 3회 루프는 조정자가 돕니다), 담당 logic=C1.
- **현재 코드 상태:** `cloud/infra/tools/`에는 `__init__.py`만 있고 `generate_infra/`·입출력 모델은 없습니다. `cloud/infra/providers/aws.py`에는 `NAME="aws"`·`REGION="ap-northeast-2"`·`STATE_LAYERS=("platform","app")` 상수만 있고, 코드 소유 틀은 docstring에 TODO(C1)로 남아 있습니다(분담상 정준우 몫). `cloud/infra/README.md`와 `registry.py` `ROLES`의 C1=유상준은 옛 표기입니다(`registry.py`는 공유 계약 파일, README는 이번 범위 밖이라 고치지 않음).
- **import 규칙:** import-linter 계약 2가 `ddak.cloud.infra.tools.generate_infra.**`에서 `ddak.core.ai` import를 허용합니다. 계약 7은 반대 방향을 막습니다: validate·plan·apply·discover·providers는 generate_infra와 `core.ai`를 import할 수 없습니다. generate_infra는 `plan/*` 등 다른 툴 디렉토리를 import하지 않고, 필요한 값은 입력·ctx로 받습니다. C-20은 같은 `cloud/infra` 안의 `ddak.cloud.infra.providers.aws`에서 읽습니다(허용 방향). 허용 목록·이름 규칙 변수·경계 변수 이름은 정준우가 넣을 예정이라(이름 🟡) 그 전까지 prompt.md에 자리표시자를 두고 fake·replay로만 진행합니다.
- **`call_ai`:** `call_ai(instruction=, data=, output_model=, prompt_version=)`을 부르면 `AIResult(value, source, usage, attempts)`가 돌아옵니다([gateway.py](../../../src/ddak/core/ai/gateway.py)). replay 키는 purpose·prompt_version·system·user(redact 뒤)·schema의 sha256입니다. 그래서 프롬프트가 한 글자만 바뀌어도 fixture를 다시 저장해야 합니다.

**코드에서 확인한 함정**
- **data는 4096자에서 잘립니다**(`build_user_prompt` → `redact(data)`, 기본 `max_len=4096`). instruction은 redact·절단이 없습니다. 허용 타입·스키마 조각·정책 규칙 문구·이름 규칙 변수 목록은 **instruction(prompt.md)**에, data에는 `infra_needs`·가린 요약·직전 오류 1줄만 넣습니다(data 속 규칙은 SYSTEM_GUARD 때문에 지시로 따르지도 않음).
- **redact가 값을 지웁니다:** `SECRET_KEY: present` 같은 `키: 값` 줄은 값이 `[REDACTED]`가 됩니다. data는 `json.dumps(..., sort_keys=True)`로 만들고 키 이름은 리스트 값(`"secret_keys": ["DATABASE_URL", "SECRET_KEY"]`)으로 두며, 이름이 redact 뒤에도 남는지 테스트합니다.
- **타임아웃·출력 길이:** 모든 AI 툴이 `DDAK_AI_TIMEOUT_S`(기본 20초) + 재시도 1회를 공유하고 timeout 인자가 없습니다(카탈로그 300초와 별개). 플랫폼 층은 넘길 가능성이 커서 `call_ai(settings=…)`로 늘릴지 정합니다. api backend `MAX_TOKENS=8192`를 넘으면 잘려서 `AI_OUTPUT_INVALID`, cli backend는 `--max-budget-usd 0.5`·`--max-turns 2`.
- **replay 키:** data 문자열 전체 + `InfraDraft` JSON 스키마(docstring·Field description 포함) + prompt 버전. data에 `run_id`·시각·임시 경로를 넣으면 매번 빗나갑니다. 수정 회차는 오류 줄 때문에 키가 달라지므로 데모 replay는 1회차 통과 응답만 믿습니다.
- **녹화 경로 없음:** `call_ai`는 `AIRequest`를 돌려주지 않아 `ReplayProvider.save(req, output)`를 바로 못 부릅니다. 안쪽 provider 호출 뒤 `save`하는 녹화 래퍼를 core/ai에 두고 사람이 돌리는 리허설 스크립트에서만 씁니다.
- **live backend:** `cli`는 구현돼 있어 오늘 본인 로컬에서 씁니다. `api`는 `anthropic`이 아직 의존성에 없습니다(`uv add anthropic` + 하네스 소유자 리뷰, `DDAK_LLM_MODEL` 필수).

**AWS·네트워크 없이 테스트하는 법**
- 단위 테스트는 [`test_ai_guard.py`](../../../harness/tests/contract/test_ai_guard.py)의 `FakeProvider` + `with tool_context("generate_infra", "run-1"):` 패턴을 씁니다. 그러려면 `cloud.py` 어댑터가 provider를 주입받아야 합니다(예: `CloudGenerateInfraAdapter(provider: LLMProvider | None = None)`).
- `adapter_mode=fake`(기본, `make run-fake`)면 `call_ai`를 아예 부르지 않습니다. replay 경로는 `adapter_mode=real` + `DDAK_LLM_BACKEND=replay`(AWS를 안 불러 비용 없음). 실제 LLM 테스트는 `@pytest.mark.llm`(`make test-llm`, 기본 `make test`에서 빠짐).
- G-2~G-4는 정준우의 C-20 틀·`.terraform.lock.hcl`·`validate_infra`와 terraform·Checkov 설치가 있어야 돌릴 수 있습니다. 그 전까지 준석님 쪽 검사는 출력 모양(파일 이름 패턴·크기 상한·필수 필드)까지만 하고, 금지 블록 검사(G-1)를 다시 만들지 않습니다.

## 3. 입력·출력 계약 (🟡 먼저 합의)

| 입력 | 출처 | 비고 |
|---|---|---|
| `run_id`, `target` | 실행기 | 필수 |
| `infra_needs`(tier·포트·DB 엔진·시크릿 키 **이름**·헬스 경로·desired 규칙) + `infra_inputs_hash` | C-17(본인 `analyze_project`) | 값·도메인·계정 ID 없음. v2는 `SECRET_KEY`가 추가되어 해시가 바뀜 |
| 환경 정보 **가린 요약** | C-22(본인 변환 함수 + 정준우 RunContext) | 키 이름·tier·리소스 종류·개수·있음/없음(예: "시크릿 `ddak/flaskr/*` 2개 있음"). `ctx.platform` 원본은 넣지 않음. 분담 계약의 "환경 정보(`terraform output`)"도 이 요약을 뜻함(✅ 장부 27, I7) |
| 리소스 허용 목록·이름 규칙 변수 / 정책 규칙 문구 / 스키마 조각 | C-20(정준우) / `prompt.md`(본인, 정준우 게이트 규칙과 맞춤) / 💭 담당 합의 | instruction에 넣음(2절 함정) |
| (개선 배포) 기존 앱 층 정보 | 저장된 승인 생성물 | 형식은 1절 0번 |
| (수정 회차) 직전 첫 오류 1줄 | `validate_infra` 출력(redact 뒤) | 1줄만 |

- 입력에는 도메인·IP·ARN·계정 ID·비밀값이 없습니다. 실제 값은 코드가 ctx에서 채웁니다(공통 계약 0절 규칙 2).
- **`infra_needs`가 오는 길:** `analyze_project` 출력 → 조정자 → `GenerateInfraInput`. 분석 결과 전체(`keys[].evidence`, 코드 줄)는 넘기지 않습니다. `secret_keys`에는 `class=secret`이면서 클라우드 시크릿으로 갈 키만(`SESSION_COOKIE_SECURE` 같은 plain 키 제외).
- **해시가 흔들리지 않게(roles/O2 §8 #9):** P1 Jev 분류는 `infra_needs`에 넣지 않고 경고로만 둡니다 💭. Jev가 plain 키를 secret으로 바꾸면(V14) 시크릿이 하나 더 생겨 "+1 ~1 -0"에서 벗어납니다.
- **`DATABASE_URL_MIGRATOR` 출처 🟡:** 앱 env 키가 아니라 분석이 못 찾습니다(roles/O2 §6-2도 "+ 💭"). `infra_needs` 규칙으로 넣을지 플랫폼 규칙으로 프롬프트에 둘지에 v1 해시와 v1 앱 층 리소스 수가 달려 있습니다.

**출력 모델 둘** (C1 §6-3 초안 + 공통 규약 1-4, 필드 💭)
- `InfraDraft`(`call_ai`의 `output_model`, AI가 채움): `files: list[{name, content}]`, `resources: list[{address, reason≤200}]`, `iam_roles: list[{name, path, boundary}]`, `assumptions: list[str≤200]`. `files`는 dict 대신 리스트(파일 이름 `^[a-z0-9_.-]+\.tf$` 검사가 쉽고, api 구조화 출력이 dict 스키마를 받는지 미확인, `make test-llm`).
- `GenerateInfraOutput`(툴 출력, 코드가 채움): `run_id`, `layer`, `bundle_sha256`, `generation_key`, `files: list[str]`(이름만, HCL은 디스크에만), `resources`, `iam_roles`, `assumptions`, `ai_usage: AIUsage | None`(replay·cache·fixture는 None), `source`(`live`/`cache`/`replay`/`fixture`), `attempts`.
- 텍스트 필드는 표시용입니다. 판정은 `validate_infra`·`plan_infra`(코드)가 합니다.
- **저장:** HCL 파일과 메타를 1절 2번에서 정한 곳에 둡니다. 팀 저장소에는 커밋하지 않습니다(N34, AGENTS.md). 단 replay fixture(`harness/fixtures/ai_replay/generate_infra/*.json`)에는 AI HCL이 들어가므로, 준석님이 직접 보고(계정 ID·ARN·도메인 없음) 본인 이름으로 커밋하고 코딩 에이전트에게 맡기지 않습니다.
- **캐시:** "승인된" 생성물만 재사용하는데 승인 기록(C-19)은 plan sha256으로 실행기 DB에 있어 generate_infra가 모릅니다. 💭 승인 뒤 조정자(또는 apply_infra)가 `var/infra/index/<generation_key>.json`에 `{bundle_sha256, plan_sha256, approved_at}`를 쓰고 generate_infra는 이것만 읽습니다.
  - 순서는 **AI 먼저, `AI_UNAVAILABLE`(타임아웃·거절 포함)이면 캐시**(✅ C1 §7), 캐시도 없으면 오류 그대로. "지연" 기준은 `DDAK_AI_TIMEOUT_S × (재시도+1)`. 캐시는 `source=cache` + 목업 고지.
  - 캐시와 replay는 **AI 호출만** 대체합니다. 게이트(G-1~G-9)와 승인은 매번 다시 합니다.

## 4. 정준우 쪽과의 경계

- **AI 파일에 쓰지 않는 것:** `terraform`·`provider`·`backend` 블록과 자격증명(버전·region·`allowed_account_ids`·`default_tags`·층별 state 키는 코드가 주입), `provisioner`, `data "external"`·`data "http"`, 로컬 밖 `module`, `aws_iam_user`·`aws_iam_access_key`, `aws_secretsmanager_secret_version`·`password` 인자, 파일 함수(`file*`·`templatefile`·`fileset`·`pathexpand`·`abspath`), 허용 목록 밖 타입. 하나라도 있으면 init 전 정적 게이트(G-1)가 거부합니다.
- **권한 경계:** 분담 계약상 모든 `aws_iam_role`에 경계 변수(이름은 C-20)가 필수입니다(플랫폼 역할 범위는 1절 8번 🟡). 신뢰 주체는 `/ddak/app/` 역할이면 `ecs-tasks` + `aws:SourceAccount`, 플랫폼 역할이면 `codebuild` 또는 같은 계정만 씁니다.
- **루프:** generate(1회) → `validate_infra`(G-1 정적 게이트 → G-2 `init -lockfile=readonly -backend=false` → G-3 `validate -json` → G-4 Checkov) → 불합격이면 첫 오류 1줄로 다시 생성합니다. **최대 3회**이고 매 회차 G-1~G-4를 전부 다시 돌립니다. 3회 안에 못 통과하면 자동 완화 없이 사람에게 넘깁니다(run `FAILED_BEFORE_DEPLOY`/`INFRA_INVALID` 💭). **사람이 HCL을 대신 쓰는 것은 대체안이 아닙니다.**
  - `validate_infra`(C1 §4-1, 필드 이름 🟡): 입력 `bundle_sha256`, 출력 `passed`·G-1~G-4 결과·`first_error`(redact 1줄). 불합격은 `passed=False`(→ 오류를 data에 넣어 재호출, 회차 +1), 수행 불가(terraform 없음 등)는 `DdakToolError`(→ 회차 안 쓰고 멈춤).
  - 🟡 함정 #4(`.arn` 참조)는 `plan_infra`의 G-6·G-7에서야 드러납니다. 💭 G-6·G-7 실패와 `AI_OUTPUT_INVALID`(스키마 불일치)도 같은 3회 안에서 1회로 세고 첫 오류 1줄로 되먹입니다.
- **그 뒤는 전부 정준우 몫입니다:** `plan_infra`(층별 plan, C-18 요약 💭 headline·counts·destructive·iam_diff(필드 이름은 공통 계약에서 정함), computed 정책이면 실패, Access Analyzer ERROR 0) → 한 화면·한 번 승인(인프라 기록은 plan sha256으로 따로 저장) → `apply_infra`(sha256 대조) → `terraform output -json`(허용 목록) → RunContext.
- **terraform 작업 디렉토리 조립도 정준우 몫 💭:** generate_infra는 AI 파일과 메타만 씁니다. 코드 소유 틀(versions·provider·backend·variables·lock)을 복사해 넣는 일은 `validate_infra`가 합니다. `bundle_sha256`은 AI 파일만으로 계산하고 틀 버전은 `generation_key`에 넣습니다.
- **두 층 state(✅):** `platform` / `app`. 앱 층은 플랫폼 층 출력을 코드가 변수로 넘겨받습니다(`terraform_remote_state` 금지). 앱 층 HCL은 플랫폼 리소스를 변수로 받는 모양이어야 합니다(💭).
- **본인 `validate_plan`과 맞물리는 곳(전부 💭, 공통 계약 6-2):** V22(인프라 요구 해시가 바뀌면 `deploy.infra.cloud` 강제 포함), V23(개선 배포에서 delete·replace가 하나라도 있거나 `/ddak/app/` 밖 IAM 변경이 있으면 BLOCK, update가 있으면 `state_change`), V24(검증 통과·AA 0·승인 기록).

## 5. 데모에서의 역할

아래 역할·시크릿 이름은 전부 💭 N44(기반 준비 전 확정, 공통 계약 12절·C1 §6-3 C-14)입니다.

**(a) 준비 단계: 플랫폼 층 1회(녹화)**
- 순서: 기반 준비(정준우, [기반 승인]) → 인프라 요구 → `generate_infra`(플랫폼 + 앱 v1) → 검증 → plan(전부 create) → [IAM·인프라 승인] → apply(플랫폼 → 앱) → 환경 정보 첫 기록. 인프라만 10~20분 [추정].
- 결과물: VPC·ALB·ECS 서비스(desired 0)·공유 RDS·CodeBuild·Docker Hub 토큰 시크릿 틀 2개, 앱 시크릿 `DATABASE_URL`·`DATABASE_URL_MIGRATOR`, 앱 역할(`ddak-flaskr-exec`·`-migrate-exec`·`-task`·`-dbinit-exec`).

**(b) 라이브: v2 앱 층 수정안** (데모는 "이미 운영 중" 상태에서 시작하고 앱 층 diff만 다룹니다 💭)
- 기대 결과 "**+ 시크릿 1, IAM 정책 1 변경, 삭제 0**"(+1 ~1 -0):
  - create 1: `aws_secretsmanager_secret` `ddak/flaskr/SECRET_KEY`(값 없음, 테스트는 `recovery_window_in_days = 0`)
  - update 1: 기존 실행 역할의 정책 리소스 **하나**(예: 인라인 `aws_iam_role_policy.exec_secrets`)에 `secretsmanager:GetSecretValue`를 추가합니다. 대상은 `…:secret:ddak/flaskr/SECRET_KEY-??????` **그 ARN 하나뿐**입니다.
  - delete·replace 0: 마지막 승인 앱 층의 기존 리소스를 그대로 유지해야 합니다(아니면 V23 BLOCK, 방법은 1절 0번).
- 값은 apply 뒤 코드가 난수로 씁니다(안승환 `sync_env_to_cloud` PutSecretValue). AI·state·화면은 값을 보지 않습니다.

| 시각 [추정] | 일 | 누구 |
|---|---|---|
| 0:14–0:24 | 분석이 `SECRET_KEY`(secret)·개발값 `dev`를 찾음 → `infra_needs` 해시 변경 | 김준석 `analyze_project` |
| **0:24–0:38** | **`generate_infra`** → `validate_infra` → `plan_infra`(+1 ~1 -0, IAM diff, AA 통과). `validate_plan`이 `deploy.infra.cloud`를 계획에 넣음(온프렘 검증 대기 없음, 10/1 밤 분리) | 김준석 → 정준우, 화면 양서윤 |
| 0:38–0:44 | 한 화면·한 번 승인, 인프라 기록(plan sha256) 따로 저장 | 양서윤 → 정준우 |
| 1:24–1:36 | 온프렘 검증 통과 → `local_verified`(클라우드는 기다리지 않음, 10/1 밤 분리) | 정준우 실행기 |
| 1:36–1:48 | `apply_infra` → 새 시크릿 ARN 기록 → 난수 PutSecretValue → `inject_env_config`(valueFrom = 새 ARN) | 정준우 → 안승환 |

- 10/1 밤 분리로 대기 지점이 없어져 위 시각은 구현 때 다시 계산합니다.
- 생성·검사·plan 창은 [추정] 하한 기준 약 14초(0:24–0:38)입니다. roles/O2 §8 #18 추정은 "인프라 제안·검사 20~110초"이고 합계가 3분을 넘을 수 있어(C1 §3-2), 리허설 실측으로 N39를 재검토합니다. live가 넘치면 승인된 생성물 캐시(`source=cache`)를 쓰고 목업이라고 밝힙니다.

## 6. 프롬프트 규칙과 AI 경계

프롬프트에 규칙 문구로 넣고, 빠지면 정적 게이트·Checkov가 다시 거부하게 합니다(AI를 믿지 않음). (💭, 원문 C1 §5-8)

| 대상 | 규칙 |
|---|---|
| 시크릿 | 틀만 만듭니다(값 없음). 앱 `ddak/flaskr/<KEY>`, 플랫폼 `ddak-platform/dockerhub-push`·`-pull`. 테스트는 `recovery_window_in_days = 0` |
| 앱 실행 역할 | `/ddak/app/` + 경계. `GetSecretValue`는 pull 토큰 + 앱 **시크릿별 ARN**(`…:secret:ddak/flaskr/<KEY>-??????`, 이름 규칙 변수로 조립). 로그 쓰기 허용. ECR 권한 없음. 정책 리소스 하나에 모읍니다(v2가 update 1이 되게) |
| 앱 태스크 역할 / DB 초기화 역할 | 태스크 역할은 비움 💭 / `rds!` 마스터 시크릿을 읽는 유일한 역할 |
| ARN 조립 | IAM 정책에서 같은 plan 리소스의 `.arn`을 참조하지 않습니다. ARN은 이름 규칙 변수 + `data.aws_caller_identity`로 조립합니다 |
| 리전·태그 | 서울 `ap-northeast-2`만. 태그는 코드의 `default_tags`가 붙이므로 AI는 쓰지 않습니다 |
| VPC·ECS | 2AZ. Fargate는 퍼블릭 서브넷 + `assign_public_ip` + SG(인바운드는 ALB SG만), RDS는 프라이빗. ECS 서비스 desired 0 + `ignore_changes = [task_definition, desired_count💭]` |
| ALB | 80 리스너 + `ignore_changes = [default_action]`, 대상 그룹 ip·8080, 헬스 값 명시. 443·301은 `ensure_tls`(1절 5번 채택 시 바뀜) |
| RDS | 공유 1개, MySQL 8.4, `require_secure_transport = 1`, 퍼블릭 금지, `manage_master_user_password = true`, `password` 금지, 테스트는 `skip_final_snapshot` |
| CodeBuild·배포 역할 | CodeBuild는 `ddak-platform/dockerhub-push` 하나만 읽고, 신뢰 정책에 `aws:SourceAccount`. `ddak-deployer`는 PassRole `role/ddak/app/*`+`ecs-tasks`, 시크릿은 Put·Describe만. ECR은 만들지 않음 |

- **AI 입력 금지(✅ 장부 27):** 비밀값, ARN·엔드포인트·IP·도메인·계정 ID, **state·plan·output 원문**. 도메인은 입력에도 출력에도 없습니다.
- `call_ai`가 redact하고 `<untrusted_data>`로 감쌉니다. 코드·diff·로그·HCL 주석 속 문장은 데이터일 뿐 지시로 따르지 않습니다.
- AI가 하지 않는 것: init·plan·apply 실행, 통과 판정, AWS 자격 보유, 이름 규칙·경계·허용 목록 변경. Jev는 HCL에 쓰지 않습니다.
- AI가 실패하거나 지연되면 캐시를 쓰고, 캐시도 없으면 사람에게 넘기며 그렇다고 알립니다. **규칙 대체 경로는 없습니다**(generate_plan과 다름).

**generate_infra가 내는 오류(`DdakToolError`, 공통 계약 11-1의 15개 안에서)**
| 상황 | 코드 |
|---|---|
| AI 타임아웃·거절·연결 실패, 캐시도 없음 | `AI_UNAVAILABLE` |
| 출력이 `InfraDraft` 스키마와 다름 / 파일 이름·크기 검사 실패 | `AI_OUTPUT_INVALID` |
| C-20 상수 없음 / `infra_needs` 비어 있음 / 개선 배포인데 마지막 승인 앱 층 번들 없음 | `PRECONDITION_FAILED` |
| `DDAK_LLM_MODEL` 없음 등 설정 오류 | `CONFIG_INVALID` |
- `INFRA_INVALID`는 오류 코드가 아니라(11-1에 없음) 루프 소진 뒤 run `FAILED_BEFORE_DEPLOY`에 조정자가 붙이는 사유입니다. 메시지에 HCL 본문·계정 ID·절대 경로를 넣지 않습니다.

## 7. 완료 기준

**혼자 끝낼 수 있는 것(💭 10/2 오전 목표)**
- [ ] 입출력 모델 합의 + `make -C harness contracts-update`(계약 변경)
- [ ] `@tool("generate_infra")` 등록, 앱 기동 등록 검사 통과
- [ ] fake(정상 1·실패 1, `source=fixture`) + FakeProvider 테스트: 정상, 스키마 불일치, 타임아웃→캐시, 캐시 없음→`AI_UNAVAILABLE`, 파일 이름 `../` 거부, 키 이름이 redact 뒤에도 남음
- [ ] `cli` backend로 v2 앱 층 live 1회 → replay fixture 저장(본인 검토·커밋)
- [ ] 출력·메시지에 비밀값·계정 ID·절대 경로 없음
- [ ] `make -C harness boundary`(import 규칙), `make -C harness ci` 통과

**정준우 선행물이 있어야 하는 것**
- [ ] v2 출력이 G-1~G-4 통과(금지 블록·파일 함수·`password`·도메인/ARN 리터럴 없음, 경계 변수): C-20 틀·`validate_infra` 필요
- [ ] v2 plan "+1 ~1 -0": 플랫폼 층과 v1 앱 층이 먼저 apply돼 있어야 함(`plan_infra`, AWS)
- [ ] 플랫폼 층 + v1 앱 층 1회 생성 → 승인 → apply(녹화): N44·C-20·기반 준비(state 버킷·경계)·`apply_infra` 필요
- [ ] 수정 루프 성공률·소요: 따로 측정하지 않고 리허설에서 나온 값만 기록(숫자 약속 없음, P1)

## 8. 알려진 함정 (C1 §8 중 generate_infra 관련)

| # | 함정 | 대응 |
|---|---|---|
| 1·2 | `data "external"`은 plan 중에 실행되고, `file()`로 `~/.aws` 캐시를 읽어 유출할 수 있음 | 아예 쓰지 않음(게이트가 init 전에 거부) |
| 3 | 학습 데이터가 옛 provider 기준(v6: `aws_eip.vpc` 제거, `data.aws_region.name` deprecated) | 스키마 조각을 프롬프트에 넣고 validate 오류를 되먹임 |
| 4 | 정책이 같은 plan의 `.arn`을 참조하면 정책이 computed → G-6 실패 | 이름 규칙 변수로 조립, 시크릿은 `name-??????` |
| 5 | AI가 시크릿마다 새 정책을 만들면 "변경"이 아니라 `+1`(create)이 되고 step 위치도 바뀜(N39) | 실행 역할 정책 리소스 하나로 모양 고정 |
| 6·7 | ECS `ignore_changes`가 없으면 태스크 정의·desired가 되돌아가고, 80 리스너 `ignore_changes`가 빠지면 301이 되돌아감 | 프롬프트 규칙 + 게이트 |
| 8 | 경계는 만든 뒤 수정 불가. 이름이 경계 패턴과 다르면 태스크 시작이 거부됨 | 이름은 코드 변수(N44)로만, AI가 이름을 짓지 않음 |
| 11 | 경계로는 신뢰 주체·과한 권한(접두사 와일드카드)을 막지 못함 | 시크릿별 ARN, 게이트 + 사람 승인 |
| 15·16 | 대상 그룹 `healthy_threshold` 기본값 불일치, `skip_final_snapshot`·복구 기간 30일 | 값 명시, 테스트 기본값을 규칙으로 |
| 22 | AI Terraform 정답률이 낮음 | 루프·캐시·승인으로 대응하고 실측 성공률만 말함 |

## 9. 일정·우선순위와 막히면

| 순서 | 할 일 | 시점 💭 |
|---|---|---|
| 1 | 1절 🟡 합의(기존 앱 층 보존·저장 위치·조정자·모델·variable/output) | 지금 |
| 2 | 앱 층 v2 수정안 오프라인(fake·FakeProvider·replay, cli live 1회) | ~10/2 오전 |
| 3 | 플랫폼 층 + v1 앱 층 생성 → 승인 → apply(정준우의 C-20·N44·기반 준비·validate/plan/apply가 먼저). **v2 plan 확인의 선행 조건**(v2 diff는 v1 앱 층 state가 있어야 plan 가능, 미루면 v2 검증도 밀림) | 10/2 |
| 4 | v2 "+1 ~1 -0" 리허설, 소요 측정 → N39 판단 재료 | 10/2 저녁 |

| 주제 | 사람 |
|---|---|
| 계약·저장 위치·이름 규칙·허용 목록·경계 변수·검증·plan·apply·조정자 | 정준우 |
| 태스크 정의에 새 시크릿 ARN 연결(`inject_env_config` valueFrom), 값 쓰기, Docker Hub 토큰, Fargate 아키텍처 | 안승환 |
| 승인 화면의 C-18 요약·`assumptions` 표시(이스케이프) | 양서윤 |

- 문서와 코드가 충돌하거나 툴 이름·스키마를 바꿔야 하면 30분 안에 `NEEDS_CONTEXT`로 묻습니다. 계약은 혼자 바꾸지 않습니다.
- 참고: 온프렘 외부 공개(Cloudflare Tunnel 확정, ✅ 10/1 밤 (2))도 준석님 몫이지만 generate_infra와는 무관합니다.

## 10. 참고 (원문)

| 문서 | 볼 곳 |
|---|---|
| [roles/C1](../roles/C1_클라우드인프라-AI테라폼-10월1일%20수정본.md) | 맨 위 📌 분담, §2 완료 기준, §3-2 시간표, §4-1·4-2, §5-1 AI 입력, §5-2 검증 루프, §5-6 비밀값, §5-8 규칙, §6-1~6-3 계약·형식, §7 AI 경계, §8 함정, §9-1 일정, §10 막히면 |
| [roles/O2](../roles/O2_분석-계획-온프렘인프라.md) | 맨 위 📌(C1 링크는 10/1 저녁 새 파일명으로 고침), §4-1 조정자·가린 요약 위치, §5-2 `call_ai`, 6절 `infra_needs`·가린 요약, §8 #9·#18, 10절 결정 표(V22 해시 범위) |
| [01 공통 계약](../01_공통-계약.md) | 0절 규칙, 1-4·1-5 툴 규약, 6-2 V22~V24, 8절 환경 정보(I7), 11-1 오류 코드, 12절 이름 규칙, 13절 비밀값, 15절 C-12·C-17·C-18·C-20·C-22 |
| [02 디렉토리 소유](../02_디렉토리-소유.md) | `cloud/infra/tools/generate_infra` 행, C1 분담 문단 |
| [10/1 결정 기록](../../../harness/docs/decisions/2026-10-01-team-status-and-decisions.md) | A절 팀 변경, B10 도메인, "C1 일" 절, E절 미결 |
| [AGENTS.md](../../../harness/AGENTS.md) · [툴 작성 규약](../../../harness/docs/harness/02_툴-작성-규약.md) | import 규칙·금지 사항·보고 형식 / C-1 구성, C-6 AI 경계, C-7 backend, C-9 DoD |
| `src/ddak/core/ai/gateway.py`, `providers/replay.py`·`api.py`, [`test_ai_guard.py`](../../../harness/tests/contract/test_ai_guard.py) | `call_ai` 시그니처, redact 4096자, replay 키, `MAX_TOKENS`, FakeProvider·`tool_context` 테스트 패턴 |
| `src/ddak/executor/service.py` `prepare`, `executor/engine.py` `_check_tool` | `subjects={"infra": plan_sha256}`, outside 툴 거부 |
| [research/IAM 최소 권한 설계](../../../research/2026-09-30_IAM-최소권한-설계.md) 4·13·14절 | 프롬프트 재료(앱 역할·경계·시크릿 장면 정책 JSON 초안) |
