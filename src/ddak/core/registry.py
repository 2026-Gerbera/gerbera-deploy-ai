"""툴 레지스트리: 툴 40개의 메타정보(카탈로그)와 구현 함수 등록(@tool 데코레이터).

기준: 앱 1개 구조(🟡 팀 합의 대기, docs/18). MCP 서버 대신 코드 안 레지스트리를 쓴다.
LangChain의 @tool과 비슷하지만 LangChain 패키지는 쓰지 않고 pydantic으로 직접 구현했다.
모듈 배치(9/30 디렉토리 정리): plan / cloud.infra / cloud.build / cd / cloud.health / verify
+ core + ops.
37개에 generate_dockerfile·validate_dockerfile(Dockerfile이 없으면 AI 생성, ✅ 9/30)과
push_image(ci, 💭)를 더해 40개다.

두 층으로 나눈다.
1. CATALOG(이 파일의 _ROWS): 40개 툴의 메타정보. 모듈, 단계, AI 사용 여부, step 층, 대상, 영향,
   담당, 플래그. 계약 파일이다(계약 변경 PR로만 고친다). 실행기·계획 검증·관리 웹은 툴 모듈을
   import하지 않고 이 표와 REGISTRY 이름 조회만 쓴다
   (AI 경계 import 계약이 간접 import로 깨지지 않게).
2. @tool("<이름>"): 담당자가 자기 디렉토리의 tool.py(예: plan/intake/tool.py,
   cd/tools/<이름>/tool.py)에서 구현 함수를 등록한다.
   등록할 때 이름(카탈로그에 있는가), 위치(카탈로그 모듈 안인가), 시그니처(입출력 pydantic 모델),
   중복을 검사한다. 어긋나면 import 시점(앱 기동)에 실패한다.

툴 모듈을 레지스트리에 채우는 곳은 조립 진입점 ddak.app 하나다(import_tools로 자동 탐색).
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import pkgutil
import typing
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType, ModuleType
from typing import Any

from pydantic import Field

from ddak.core.contracts.base import ContractModel, ToolInput
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Effect, Layer, Module, Stage, Target, ToolKind

PING = "ping"  # 예시 툴. 40개 목록 밖이다(canonical=False).

# 역할 코드 -> 실명(✅ 장부 18). 코드에는 역할 코드만 쓰고 화면 표시에만 이 표를 쓴다.
ROLES: Mapping[str, str] = MappingProxyType(
    {
        "C1": "유상준",  # 클라우드 인프라: AI Terraform 툴·정책 검사·권한 경계·IAM 승인·HTTPS
        "C2": "안승환",  # 클라우드 빌드·배포(CodeBuild·Docker Hub 푸시·ECS·RDS·Secrets Manager)
        "C3": "양서윤",  # 클라우드 검증 + 관리 페이지(승인 화면, 💭 Slack 알림)
        "O1": "정준우",  # 온프렘 런타임 + 실행기
        "O2": "김준석",  # 입구·분석 + 계획·계획 검증 + 온프렘 인프라 프로비저닝
        "O3": "장민영",  # 샘플 앱 + 교차 검증 + (토글) AI 패치 + 💭 Dockerfile 생성·검사
        "TL": "미지정",  # 하네스·계약 승인자. 테크 리드는 공식 지정 없음(팀 결정 필요)
    }
)


class ToolSpec(ContractModel):
    """툴 하나의 메타정보. contracts/schemas/tool_catalog.json으로 내보낸다."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    module: Module
    stage: Stage
    kind: ToolKind
    uses_ai: bool  # call_ai·Jev를 부를 수 있는가(런타임 가드 기준)
    layer: Layer  # 기본 step 층. step 카탈로그가 대상별로 덮어쓸 수 있다
    targets: tuple[Target, ...] = ()  # () = target 없음
    effect: Effect = Effect.READ  # 💭 기본 영향. step 카탈로그가 step별로 정한다
    owners: dict[str, str]  # {"logic": "O2", "local": "O1", "cloud": "C2", "ui": "C3"}
    destructive: bool = False
    read_only: bool = False
    requires_lock: bool = False
    requires_approval: bool = False
    timeout_s: int = Field(default=60, gt=0)  # 💭 실행기 타임아웃(실측 전 값)
    canonical: bool = True  # False = 40개 밖(예시 툴 ping)


_PL, _IN, _BU, _CD, _VE = Module.PLAN, Module.INFRA, Module.BUILD, Module.CD, Module.VERIFY
_CH, _CO, _OP = Module.CLOUD_HEALTH, Module.CORE, Module.OPS
_S, _L = Stage, Layer

# (이름, 모듈, 단계, 층, 플래그, 대상, 담당)
# 플래그: ai=uses_ai, ro=read_only, de=destructive, lock=requires_lock, ok=requires_approval
# 대상: both=local+cloud, cloud, ""=target 없음
# 담당: "공통/local/cloud/ui" 순서의 역할 코드, "-"는 없음. 실명은 ROLES
_ROWS: tuple[tuple[str, Module, Stage, Layer, str, str, str], ...] = (
    # ── plan: ① 플랜 + ② 계획 검증. 계획을 만드는 쪽이라 step 층은 "계획 밖"
    ("receive_deploy_request", _PL, _S.PLAN, _L.OUTSIDE, "", "", "O2/-/-/C3"),
    ("analyze_project", _PL, _S.PLAN, _L.OUTSIDE, "ai ro", "", "O2"),  # 환경 키 분류(Jev/Claude)
    ("detect_changed_tiers", _PL, _S.PLAN, _L.OUTSIDE, "ro", "", "O2"),
    ("generate_plan", _PL, _S.PLAN, _L.OUTSIDE, "ai ro", "", "O2"),  # step 선택(Jev/Claude)
    # generate_dockerfile: Dockerfile이 없을 때만 AI가 초안을 만든다(✅ 9/30, 조건: 없을 때).
    # 검사(validate_dockerfile)·사람 승인 뒤 저장·재사용. 담당은 💭
    ("generate_dockerfile", _PL, _S.PLAN, _L.OUTSIDE, "ai ro", "", "O3"),
    # patch_*: 코드 수정 토글 ON일 때만(✅ 장부 16: 기본 OFF, 보고 우선, AI 패치는 나중)
    ("patch_db_access", _PL, _S.PLAN, _L.OUTSIDE, "ai ok", "", "O3"),
    ("patch_storage", _PL, _S.PLAN, _L.OUTSIDE, "ai ok", "", "O3"),
    ("patch_config", _PL, _S.PLAN, _L.OUTSIDE, "ai ok", "", "O3"),
    # ② 계획 검증: 결정적 검사기. AI·생성기 import 금지(import-linter 계약 5)
    ("validate_plan", _PL, _S.VALIDATE, _L.OUTSIDE, "ro", "", "O2"),
    # validate_dockerfile: 정적 검사(hadolint 등 + 보안 기본값) + 실제 빌드 성공 확인(push 없음)
    ("validate_dockerfile", _PL, _S.VALIDATE, _L.OUTSIDE, "", "", "O3/-/-/C3"),
    # ── cloud.infra: AI Terraform(✅ 장부 21~23). 툴 이름·개수·배치는 💭.
    # AI는 HCL 초안만 만들고(generate_infra), 탐지·검사·plan·apply는 코드다(계약 7).
    ("discover_existing", _IN, _S.PLAN, _L.OUTSIDE, "ro", "cloud", "C1"),  # 읽기 전용 탐지
    ("generate_infra", _IN, _S.PLAN, _L.OUTSIDE, "ai ro", "cloud", "C1"),  # AI HCL 초안(제안만)
    ("validate_infra", _IN, _S.VALIDATE, _L.OUTSIDE, "ro", "cloud", "C1"),  # init 전 정적 게이트 등
    ("plan_infra", _IN, _S.VALIDATE, _L.OUTSIDE, "ro", "cloud", "C1/-/C1/C3"),  # plan 요약·AA
    # apply_infra: 사람 승인(plan 파일 sha256에 묶음) 뒤에만 terraform apply. 자동 apply 없음(💭).
    # 옛 ensure_infra(존재 확인만)를 대체한다. 온프렘 점검은 preflight_check(local)로 옮긴다(💭)
    ("apply_infra", _IN, _S.DEPLOY, _L.CONDITIONAL, "lock de ok", "cloud", "C1/-/C1/C3"),
    # ── cloud.build: ③ 빌드. CodeBuild -> 이미지 저장소(기본 Docker Hub, ECR은 옵션, ✅ 9/30).
    # 온프렘은 같은 digest를 Docker Hub에서 읽기 전용 토큰으로 pull(AWS 자격 불필요)
    ("build_image", _BU, _S.BUILD, _L.CONDITIONAL, "", "both", "C2/O1/C2"),
    # push_image(💭): 저장소 어댑터로 push된 digest를 확인·기록(CodeBuild 경로는 빌드 안에서 push)
    ("push_image", _BU, _S.BUILD, _L.BUILTIN, "", "", "C2"),
    # ── cd: ④ 배포. 공통 인터페이스(deploy·rollback·health_check·migrate_db·inject_config·
    # ensure_tls) + providers/aws·onprem(✅ 9/30). 툴이 target으로 provider를 고른다(AI 아님)
    ("acquire_deploy_lock", _CD, _S.DEPLOY, _L.BUILTIN, "", "", "O1"),  # lock_token 발급
    ("inject_env_config", _CD, _S.DEPLOY, _L.CONDITIONAL, "lock", "both", "O1/O1/C2"),
    # sync_env_to_cloud: SECRET_KEY는 시스템 난수(✅ 장부 13). Terraform이 만든 빈 시크릿에
    # PutSecretValue(값은 state·AI·화면에 없음). 승인은 배포 클릭으로 갈음(💭)
    ("sync_env_to_cloud", _CD, _S.DEPLOY, _L.CONDITIONAL, "lock de", "cloud", "C2/-/C2"),
    ("prepare_db", _CD, _S.DEPLOY, _L.CONDITIONAL, "lock de", "both", "O1/O1/C2"),  # 추가형만
    ("prepare_storage", _CD, _S.DEPLOY, _L.OPTIONAL, "lock", "both", "O1/O1/C2"),  # 골든 패스 제외
    # ensure_tls·verify_tls: 클라우드만(✅ 장부 17). 온프렘 provider는 "해당 없음"을 돌려준다.
    # ensure_tls 툴은 cd에 두고 AWS 구현은 cloud/tls(유상준)에 둔다
    ("ensure_tls", _CD, _S.DEPLOY, _L.MANDATORY, "lock", "cloud", "C1/-/C1"),
    ("deploy_tier", _CD, _S.DEPLOY, _L.CONDITIONAL, "lock de", "both", "O1/O1/C2"),
    ("rollback_tier", _CD, _S.DEPLOY, _L.BUILTIN, "lock de", "both", "O1/O1/C2"),  # 실패 처리기
    # health_check: ⑤ 단계지만 CD 인터페이스 함수라 cd 모듈(💭 트리)
    ("health_check", _CD, _S.VERIFY_REPORT, _L.MANDATORY, "ro", "both", "O3/O3/C3"),
    # ── verify: ⑤ 검증 및 보고
    ("smoke_test", _VE, _S.VERIFY_REPORT, _L.MANDATORY, "", "both", "O3/O3/C3"),  # 테스트 데이터
    # verify_tls: 클라우드 헬스·TLS 검증 디렉토리(cloud/health, 양서윤)에 산다
    ("verify_tls", _CH, _S.VERIFY_REPORT, _L.MANDATORY, "ro", "cloud", "C3/-/C3"),
    ("compare_env_results", _VE, _S.VERIFY_REPORT, _L.MANDATORY, "ro", "", "O3"),
    ("watch_post_deploy", _VE, _S.VERIFY_REPORT, _L.OPTIONAL, "ro", "both", "C3/O1/C3"),
    ("collect_diagnostics", _VE, _S.VERIFY_REPORT, _L.BUILTIN, "ro", "both", "C3/O1/C3"),
    ("diagnose_parity_gap", _VE, _S.VERIFY_REPORT, _L.BUILTIN, "ai ro", "", "O3"),  # 원인 설명
    ("record_deploy_log", _VE, _S.VERIFY_REPORT, _L.BUILTIN, "", "", "O1"),  # 입력 redact
    ("post_report", _VE, _S.VERIFY_REPORT, _L.MANDATORY, "ai ro", "", "C3"),  # AI 요약은 선택
    # ── 공통 (core). call_ai는 관문 자체라 ai 플래그가 없다(ai = 관문을 부르는 툴)
    ("call_ai", _CO, _S.COMMON, _L.OUTSIDE, "", "", "O2"),
    ("request_approval", _CO, _S.COMMON, _L.BUILTIN, "", "", "O1/-/-/C3"),  # 배포 클릭
    ("stream_progress", _CO, _S.COMMON, _L.BUILTIN, "", "", "O1/-/-/C3"),  # JSONL -> SSE
    # ── 운영 (ops). 계획에 넣지 않는다. cleanup은 사람만 실행한다
    ("preflight_check", _OP, _S.OPS, _L.OUTSIDE, "ro", "both", "O1/O1/C1"),
    ("reset_demo_state", _OP, _S.OPS, _L.OUTSIDE, "de ok", "both", "O1/O1/C2"),
    ("cleanup", _OP, _S.OPS, _L.OUTSIDE, "de ok", "both", "C1/O1/C1"),
)

# 💭 기본 영향. 나머지는 READ. step별 값(예: ensure_tls mode=check는 read)은 step 카탈로그가 정한다.
_EFFECTS: Mapping[str, Effect] = {
    "build_image": Effect.ADDITIVE_PREP,
    "push_image": Effect.ADDITIVE_PREP,  # 저장소에 새 digest(기존 태그는 덮어쓰지 않음)
    # apply_infra: 기본은 상태 변경. plan이 create뿐이면(부트스트랩) step 카탈로그가 additive_prep.
    # 개선 배포에 in-place update(예: v2 시크릿 장면의 실행 역할 정책 변경)가 있으면 G1 뒤(V23)
    "apply_infra": Effect.STATE_CHANGE,
    "sync_env_to_cloud": Effect.ADDITIVE_PREP,  # 값이 없을 때만 PutSecretValue
    "inject_env_config": Effect.STATE_CHANGE,  # 온프렘 .env. cloud는 새 리비전 등록(additive)
    "prepare_db": Effect.STATE_CHANGE,
    "prepare_storage": Effect.STATE_CHANGE,
    "ensure_tls": Effect.STATE_CHANGE,  # "도메인 연결" run에서만 변경, 데모 run은 확인 모드
    "deploy_tier": Effect.STATE_CHANGE,
    "rollback_tier": Effect.STATE_CHANGE,
    "reset_demo_state": Effect.STATE_CHANGE,
    "cleanup": Effect.STATE_CHANGE,
}
# 💭 실측 전 타임아웃(초). 나머지는 60초.
_TIMEOUTS: Mapping[str, int] = {
    "build_image": 900,  # CodeBuild 프로비저닝 + 빌드 + push(Docker Hub)
    "push_image": 120,  # 저장소 API로 digest 확인
    "generate_dockerfile": 120,  # AI Dockerfile 초안 1회
    "validate_dockerfile": 600,  # 정적 검사 + 로컬 검증 빌드(push 없음)
    "generate_infra": 600,  # 긴 HCL 응답과 API 재시도를 포함한 AI 초안 1회
    "validate_infra": 300,  # 정적 게이트 + init(미러) + validate + 정책 검사
    "plan_infra": 600,  # 읽기 세션 plan + show -json + Access Analyzer
    "apply_infra": 3600,  # RDS 생성 최대 20분(AWS 문서). provider 기본 create 타임아웃 40분
    "prepare_db": 300,  # Fargate cold start를 포함한 precheck·up·verify 3단계
    "deploy_tier": 300,
    "rollback_tier": 300,
    "ensure_tls": 2700,  # "도메인 연결" run의 ACM 발급 상한(45분). 데모 run은 툴이 60초로 자름
    "health_check": 120,
    "smoke_test": 120,
}
_FLAGS = {"ai", "ro", "de", "lock", "ok"}
_TARGETS = {"": (), "both": (Target.LOCAL, Target.CLOUD), "cloud": (Target.CLOUD,)}
_OWNER_KEYS = ("logic", "local", "cloud", "ui")
_KINDS = {
    "call_ai": ToolKind.LIBRARY,
    "request_approval": ToolKind.EXECUTOR_FN,
    "stream_progress": ToolKind.EXECUTOR_FN,
}


def _spec(
    row: tuple[str, Module, Stage, Layer, str, str, str], *, canonical: bool = True
) -> ToolSpec:
    name, module, stage, layer, flag_text, target_text, owner_text = row
    flags = set(flag_text.split())
    if flags - _FLAGS:
        raise ValueError(f"{name}: 모르는 플래그 {sorted(flags - _FLAGS)}")
    owners = {k: v for k, v in zip(_OWNER_KEYS, owner_text.split("/"), strict=False) if v != "-"}
    return ToolSpec(
        name=name,
        module=module,
        stage=stage,
        kind=_KINDS.get(name, ToolKind.TOOL_FN),
        uses_ai="ai" in flags,
        layer=layer,
        targets=_TARGETS[target_text],
        effect=_EFFECTS.get(name, Effect.READ),
        owners=owners,
        destructive="de" in flags,
        read_only="ro" in flags,
        requires_lock="lock" in flags,
        requires_approval="ok" in flags,
        timeout_s=_TIMEOUTS.get(name, 60),
        canonical=canonical,
    )


_PING_ROW = (PING, _CO, _S.COMMON, _L.OPTIONAL, "ro", "both", "TL")
CATALOG: tuple[ToolSpec, ...] = (
    *(_spec(row) for row in _ROWS),
    _spec(_PING_ROW, canonical=False),
)


# ---------------------------------------------------------------------------
# 등록
# ---------------------------------------------------------------------------
class RegistryError(RuntimeError):
    """등록하려는 툴이 카탈로그·위치·시그니처 규칙과 맞지 않는다(앱 기동 실패)."""


class UnknownToolError(KeyError):
    """카탈로그에 없거나 아직 구현이 등록되지 않은 툴."""


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    fn: Callable[..., Any]
    input_model: type[ToolInput]
    output_model: type[ContractModel]
    is_async: bool


def _signature(fn: Callable[..., Any], name: str) -> tuple[type[ToolInput], type[ContractModel]]:
    """def <tool>(inp: <ToolInput 하위>, ctx: RunContext) -> <ContractModel 하위>."""
    params = list(inspect.signature(fn).parameters)
    if params != ["inp", "ctx"]:
        raise RegistryError(f"{name}: 인자는 (inp, ctx) 두 개여야 한다(지금: {params})")
    try:
        hints = typing.get_type_hints(fn)
    except NameError as exc:  # TYPE_CHECKING 블록 안에서 import한 타입
        raise RegistryError(
            f"{name}: 타입 주석을 풀 수 없다(모듈 최상단에서 import): {exc}"
        ) from exc
    inp, ctx, out = hints.get("inp"), hints.get("ctx"), hints.get("return")
    if not (isinstance(inp, type) and issubclass(inp, ToolInput)):
        raise RegistryError(f"{name}: inp 타입은 <Tool>Input(ToolInput 하위)이어야 한다")
    if ctx is not RunContext:
        raise RegistryError(f"{name}: ctx 타입은 RunContext여야 한다")
    if not (isinstance(out, type) and issubclass(out, ContractModel)):
        raise RegistryError(f"{name}: 반환 타입은 <Tool>Output(ContractModel 하위)이어야 한다")
    return inp, out


class Registry:
    """카탈로그 + 구현 함수 묶음. 앱에는 REGISTRY 하나, 테스트는 따로 만들어 쓴다."""

    def __init__(self, catalog: Iterable[ToolSpec], *, package: str | None = None) -> None:
        specs: dict[str, ToolSpec] = {}
        for spec in catalog:
            if spec.name in specs:
                raise ValueError(f"카탈로그에 이름이 두 번 있다: {spec.name}")
            specs[spec.name] = spec
        self._specs: Mapping[str, ToolSpec] = MappingProxyType(specs)
        self._impls: dict[str, RegisteredTool] = {}
        self._package = package  # 설정하면 등록 위치를 ddak.<모듈>. 아래로 강제한다

    # ---- 조회 ----
    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def spec(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise UnknownToolError(name) from None

    def get(self, name: str) -> RegisteredTool:
        """실행기가 부르는 유일한 경로. 구현이 등록되지 않았으면 UnknownToolError."""
        try:
            return self._impls[name]
        except KeyError:
            raise UnknownToolError(name) from None

    def registered(self) -> frozenset[str]:
        return frozenset(self._impls)

    def missing(self) -> frozenset[str]:
        """카탈로그상 등록 함수여야 하는데 아직 구현이 없는 툴(구현 진행 상황)."""
        return frozenset(
            s.name
            for s in self._specs.values()
            if s.kind is ToolKind.TOOL_FN and s.canonical and s.name not in self._impls
        )

    # ---- 등록 ----
    def tool[F: Callable[..., Any]](self, name: str) -> Callable[[F], F]:
        """@tool("prepare_db"): 구현 함수 등록 데코레이터."""
        try:
            spec = self.spec(name)
        except UnknownToolError:
            raise RegistryError(f"카탈로그에 없는 툴: {name}") from None
        if spec.kind is not ToolKind.TOOL_FN:
            raise RegistryError(f"{name}은 등록 함수가 아니다({spec.kind.value})")

        def decorator(fn: F) -> F:
            if self._package is not None:
                prefix = f"{self._package}.{spec.module.value}."
                if not fn.__module__.startswith(prefix):
                    raise RegistryError(
                        f"{name}은 {spec.module.value} 모듈 소속이다(등록 위치: {fn.__module__})"
                    )
            inp, out = _signature(fn, name)
            if name in self._impls:
                raise RegistryError(f"{name}을 두 번 등록했다")
            self._impls[name] = RegisteredTool(
                spec=spec,
                fn=fn,
                input_model=inp,
                output_model=out,
                is_async=inspect.iscoroutinefunction(fn),
            )
            return fn

        return decorator


REGISTRY = Registry(CATALOG, package="ddak")
tool = REGISTRY.tool


def spec_for(name: str) -> ToolSpec:
    return REGISTRY.spec(name)


def canonical_names() -> frozenset[str]:
    """40개 고정 이름."""
    return frozenset(s.name for s in CATALOG if s.canonical)


def ai_tools() -> frozenset[str]:
    """call_ai·Jev를 부를 수 있는 툴 이름. 9개(tests/contract/test_registry.py가 고정).

    generate_infra(AI Terraform 초안)로 7 -> 8, generate_dockerfile(AI Dockerfile 초안)로 8 -> 9.
    채팅 의도 JSON을 AI 툴로 등록하면 10개가 된다(이름·위치는 결정 필요, docs/harness/06).
    """
    return frozenset(s.name for s in CATALOG if s.uses_ai)


def tools_in(module: Module) -> tuple[ToolSpec, ...]:
    return tuple(s for s in CATALOG if s.module is module and s.canonical)


def import_tools(package: ModuleType) -> list[str]:
    """package 바로 아래 <디렉토리>/tool.py를 이름순으로 import한다(import 시 @tool이 등록한다).

    공유 등록 목록을 두지 않아서 담당자끼리 같은 파일을 고치지 않는다(머지 충돌 방지).
    이름이 '_'로 시작하는 디렉토리와 아직 tool.py가 없는 디렉토리(빈 구현)는 건너뛴다.
    ddak.app만 부른다.
    """
    loaded: list[str] = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if not info.ispkg or info.name.startswith("_"):
            continue
        name = f"{package.__name__}.{info.name}.tool"
        if importlib.util.find_spec(name) is None:
            continue
        importlib.import_module(name)
        loaded.append(info.name)
    return loaded
