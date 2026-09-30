"""plan.json(최종 계획). validate_plan(조립기, 코드)이 만들고 실행기가 이것만 실행한다.

💭 초안: 필드 확정은 계약 문서(docs/contracts, TODO(contract)).
근거는 설계 문서 02 파이프라인과 계획 6절(single-app/docs/02). 하네스는 실행기 최소 예시에 필요한
모양만 둔다. 골든 패스 v2 예시는 fixtures/plans/golden_v2_update.json(파싱 테스트 있음).

- 단계 순서(빌드 -> 배포 -> 검증)는 고정이다. 단계 안 step은 계획(AI)이 넣고 뺀다(✅ 장부 5).
- 섹션: build / deploy.local / deploy.cloud / verify.
  각 섹션은 steps(실행 순서)와 skipped(뺀 것과 이유).
- wait_for / signal은 step 카탈로그 값이다. AI가 써도 조립기가 덮어쓴다. 신호 이름은 고정 4개.
  infra_ready(💭)는 인프라 apply step(deploy.infra.cloud)이 있을 때만 쓴다. 부트스트랩에서는
  빌드가 이 신호를 기다린다(CodeBuild 프로젝트가 apply로 생기기 때문. ECR 옵션이면 ECR도).
- 도메인·호스트·이미지 digest·시크릿 값은 plan.json에 없다.
  실행 컨텍스트(RunContext)에서 코드가 채운다.
- AI 초안(PlanDraft)은 plan 모듈이 정의한다(조건부·선택 step 결정만 담는다).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ddak.core.contracts.base import ContractModel, RunId, TierName
from ddak.core.contracts.enums import By, Effect, Layer, RunMode, Source, Target

PLAN_SCHEMA = "ddak.plan/v1"  # 💭 접두어 ddak은 가칭

INFRA_READY = "infra_ready"  # 💭 인프라 apply 끝(사람 승인 뒤 terraform apply, 출력 기록)
IMAGES_READY = "images_ready"  # G0: 빌드 끝(digest 기록)
LOCAL_VERIFIED = "local_verified"  # G1: 온프렘 헬스·스모크 통과
CLOUD_VERIFIED = "cloud_verified"  # 클라우드 헬스·TLS·스모크 통과
SignalName = Literal["infra_ready", "images_ready", "local_verified", "cloud_verified"]

# 💭 step id 형식: <단계>.<대상물>[.<트랙>]  예) build.was, deploy.db.cloud, verify.compare
STEP_ID_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,3}$"


class PlanStep(ContractModel):
    """실행할 step 하나. step != 툴: 같은 툴이 여러 step에 쓰인다(deploy_tier가 tier·대상별로)."""

    id: str = Field(pattern=STEP_ID_PATTERN)
    tool: str = Field(pattern=r"^[a-z][a-z0-9_]*$")  # 레지스트리 이름
    target: Target | None = None  # 트랙과 같아야 한다(빌드·검증 공통 step은 None)
    tier: TierName | None = None
    # step별 허용 키만(예: migrations, scenarios, mode, keys).
    # 허용 목록은 step 카탈로그(validate_plan)가 정한다.
    # domain·host·url·zone·image·digest·arn·command 같은 키는 계획 검증이 거부한다(V3).
    params: dict[str, Any] = Field(default_factory=dict)
    layer: Layer
    effect: Effect = Effect.READ  # step 카탈로그 값(예: ensure_tls mode=check는 read)
    by: By = By.RULE
    reason: str | None = Field(default=None, max_length=200)  # 표시용. 판정에 쓰지 않는다
    evidence: list[str] = Field(default_factory=list)  # 예) fact:tree_changed.was
    wait_for: list[SignalName] = Field(default_factory=list)  # 항상 배열(문자열 하나도 배열로)
    signal: SignalName | None = None
    # "finally": verify 섹션에서만. 앞 step·트랙이 실패해도 마지막에 실행한다(보고, verify.report).
    # 실패해도 run 결과를 바꾸지 않는다(설계 문서 02 8-3 "보고 실패 = 결과 유지").
    run: Literal["finally"] | None = None


class SkippedStep(ContractModel):
    """뺀 step과 이유. 관리 페이지 계획 카드의 "제외" 줄."""

    id: str = Field(pattern=STEP_ID_PATTERN)
    tool: str
    target: Target | None = None
    tier: TierName | None = None
    layer: Layer
    by: By = By.RULE
    reason: str = Field(max_length=200)
    skip_rule: str | None = None  # 결정적 규칙 id(예: tree_unchanged, digest_deployed)


class Section(ContractModel):
    steps: list[PlanStep] = Field(default_factory=list)
    skipped: list[SkippedStep] = Field(default_factory=list)
    signal: SignalName | None = None  # 섹션이 끝나면 여는 신호(build -> images_ready)


class DeploySections(ContractModel):
    local: Section = Field(default_factory=Section)
    cloud: Section = Field(default_factory=Section)


class Planner(ContractModel):
    by: By
    provider: str  # jev | claude-api | claude-cli | replay | rule
    model: str | None = None
    source: Source = Source.LIVE
    attempts: int = Field(default=1, ge=0)
    fallback: bool = False  # 재지시 1회 뒤 규칙 계획으로 물러났는가


class Invalidated(ContractModel):
    """AI가 빼려 했으나 계획 검증이 무효화한 것(강제 포함)."""

    id: str
    attempt: Literal["skip"] = "skip"
    by: By = By.AI
    result: Literal["forced_include"] = "forced_include"
    why: str = Field(max_length=200)


class PlanWarning(ContractModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(max_length=200)


class Plan(ContractModel):
    schema_: Literal["ddak.plan/v1"] = Field(default=PLAN_SCHEMA, alias="schema")
    run_id: RunId
    project: str = ""
    mode: RunMode = RunMode.UPDATE
    facts_hash: str | None = None  # 실행 시작 때 재계산, 다르면 재계획(V11)
    plan_hash: str | None = None  # DeploymentService가 매 step 직전 대조(V12)
    planner: Planner | None = None
    toggles: dict[str, bool] = Field(default_factory=lambda: {"code_patch": False})
    build: Section = Field(default_factory=Section)
    deploy: DeploySections = Field(default_factory=DeploySections)
    verify: Section = Field(default_factory=Section)
    invalidated: list[Invalidated] = Field(default_factory=list)
    warnings: list[PlanWarning] = Field(default_factory=list)
