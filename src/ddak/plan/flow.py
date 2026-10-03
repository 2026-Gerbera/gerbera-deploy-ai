"""플랜 흐름: 접수 -> 탐지 -> 분석 -> 계획 생성 -> 검증을 한 호출로 잇는다(O2 구간의 끝).

산출물 PlanBundle(+ detect.facts_reader)이 DeploymentService.prepare 입력이다.
ddak.core.ai는 import하지 않는다(analyze·planner가 부른다). 여기서는 tool_context만 건다.
"""

from __future__ import annotations

import secrets
import shutil
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Plan
from ddak.core.contracts.plan_draft import PlanDraft
from ddak.core.contracts.plan_facts import Env, Facts, FileMeta
from ddak.core.contracts.tools.analyze_project import AnalyzeProjectInput
from ddak.core.contracts.tools.detect_changed_tiers import DetectChangedTiersInput
from ddak.core.contracts.tools.generate_plan import GeneratePlanInput
from ddak.core.contracts.tools.patch_config import PatchConfigOutput
from ddak.core.contracts.tools.receive_deploy_request import ReceiveDeployRequestInput
from ddak.core.contracts.tools.validate_plan import ValidatePlanInput
from ddak.core.logging import get_logger
from ddak.core.patch_patterns import iter_source_texts
from ddak.core.runtime import tool_context
from ddak.core.storage import (
    OUTPUT_KEY,
    STORAGE_SMOKE_GROUP,
    scan_storage,
    storage_intent,
)
from ddak.plan.analyze import analyze_project
from ddak.plan.detect import detect_changed_tiers
from ddak.plan.intake import FetchPolicy, cleanup_stale_sources, receive_deploy_request
from ddak.plan.planner import generate_plan
from ddak.plan.validate import validate_plan

__all__ = ["PlanBundle", "new_run_id", "plan_deployment"]

_log = get_logger("plan")
_ENVS: tuple[Env, ...] = ("local", "cloud")


@dataclass(frozen=True)
class PlanBundle:
    """prepare에 넘길 순수 데이터. facts_reader(detect.facts_reader)는 호출자가 따로 넘긴다."""

    plan: Plan
    context: RunContext
    facts: Facts
    source: Path
    patch: bytes | None = None
    patch_meta: dict[str, Any] | None = None
    patch_review: PatchConfigOutput | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.model_dump(mode="json", by_alias=True),
            "context": self.context.to_json_dict(),
            "facts": self.facts.model_dump(mode="json"),
            "source": str(self.source),
        }


def new_run_id(now: datetime | None = None) -> str:
    """run-YYYYMMDD-HHMMSS-<4hex>. 시각 부분은 now가 정하고 뒤 4자리만 무작위."""
    now = now or datetime.now(UTC)
    return f"run-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


@contextmanager
def _stage(
    name: str, run_id: str, record_stage: Callable[[str, int, str], None] | None = None
) -> Iterator[None]:
    t0 = time.perf_counter()
    status = "succeeded"
    try:
        yield
    except BaseException:
        status = "failed"
        _log.warning("플랜 단계 실패", run_id=run_id, stage=name, ms=_ms(t0))
        raise
    finally:
        if record_stage is not None:
            record_stage(name, _ms(t0), status)
    _log.info("플랜 단계 완료", run_id=run_id, stage=name, ms=_ms(t0))


def _ms(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


def plan_deployment(
    request: DeployRequest,
    *,
    run_id: str,
    settings: Settings,
    strict_ai_check: bool = False,
    previous_manifests: Callable[[str], Mapping[Env, Mapping[str, FileMeta] | None]],
    cloud_domain: str | None = None,
    platform: Mapping[str, Any] | None = None,
    fetch_policy: FetchPolicy | None = None,
    fetcher: Any = None,
    jev_client: Any = None,
    provider: Any = None,
    patch_preparer: Callable[[Path, Facts, RunContext], Any] | None = None,
    record_stage: Callable[[str, int, str], None] | None = None,
    source_context: RunContext | None = None,
) -> PlanBundle:
    policy = fetch_policy or FetchPolicy.from_env()
    cleanup_stale_sources(policy.root, policy.cache_ttl_s, keep=(run_id,))
    t_all = time.perf_counter()
    base_context = source_context or RunContext(run_id, build_backend=settings.build_backend)
    ctx = replace(
        base_context,
        run_id=run_id,
        adapter_mode=settings.adapter_mode,
        project=request.project,
        mode=request.mode,
        repo_url=request.repo_url,
        toggles={
            **base_context.toggles,
            "code_patch": request.code_patch,
            "strict_ai_check": strict_ai_check,
        },
        cloud_domain=cloud_domain if cloud_domain is not None else base_context.cloud_domain,
        platform=dict(platform if platform is not None else base_context.platform),
        project_settings={
            **{
                key: value
                for key, value in (
                    ("aws_profile", settings.aws_profile),
                    ("aws_expected_account_id", settings.aws_expected_account_id),
                )
                if value is not None
            },
            **base_context.project_settings,
        },
    )

    with _stage("intake", run_id, record_stage):
        got = receive_deploy_request(
            ReceiveDeployRequestInput(run_id=run_id, request=request),
            ctx,
            policy=policy,
            fetcher=fetcher,
        )
    checkout = policy.root / got.source_dir
    try:  # 접수 이후 실패는 run 사본을 지우고 원래 예외를 다시 던진다
        ctx = replace(
            ctx,
            deploy_config=got.deploy_config,
            source_sha=got.commit,
            ref=base_context.ref if base_context.ref is not None else request.ref,
        )
        previous = previous_manifests(request.project)
        prev: dict[Env, dict[str, FileMeta] | None] = {
            e: (None if previous.get(e) is None else dict(previous[e] or {}))
            for e in (_ENVS if request.target == "both" else (request.target,))
        }

        with _stage("detect", run_id, record_stage):
            det = detect_changed_tiers(
                DetectChangedTiersInput(
                    run_id=run_id,
                    request=request,
                    source_dir=got.source_dir,
                    snapshot=got.snapshot,
                    previous=prev,
                ),
                ctx,
                root=policy.root,
            )
        with _stage("analyze", run_id, record_stage), tool_context("analyze_project", run_id):
            ana = analyze_project(
                AnalyzeProjectInput(
                    run_id=run_id,
                    request=request,
                    source_dir=got.source_dir,
                    changed=det.changed,
                    new_migrations=det.new_migrations,
                    changed_paths=det.changed_paths,
                ),
                ctx,
                jev_client=jev_client,
                root=policy.root,
            )
        storage = storage_intent(
            STORAGE_SMOKE_GROUP in ana.smoke_groups,
            bool(ctx.platform.get("cloud", {}).get(OUTPUT_KEY)),
        )
        project_settings = dict(ctx.project_settings)
        project_settings.pop("_infra_storage", None)
        if request.target in {"cloud", "both"} and storage is not None:
            project_settings["_infra_storage"] = {
                "intent": storage,
                "evidence": [
                    asdict(e)
                    for e in scan_storage(dict(iter_source_texts(checkout, python_only=True)))
                ],
                # 생성 이름은 읽기 세션을 가진 조립부가 순번을 예약한 뒤 채운다.
                "bucket": ctx.platform.get("cloud", {}).get(OUTPUT_KEY)
                if storage == "remove"
                else None,
            }
        ctx = replace(ctx, project_settings=project_settings)
        facts = Facts(
            project=request.project,
            mode=request.mode,
            target=request.target,
            tiers=ana.tiers,
            changed=det.changed,
            new_migrations=det.new_migrations,
            modified_migrations=det.modified_migrations,
            env_keys=ana.env_keys,
            patch_targets=ana.patch_targets,
            smoke_groups=ana.smoke_groups,
            db_initialized={e: prev[e] is not None for e in prev},
            has_dockerfile=ana.has_dockerfile,
            infra_inputs_changed=ana.infra_inputs_changed,
            code_patch=request.code_patch,
            source_snapshot_hash=got.snapshot.source_snapshot_hash,
            facts_hash=det.facts_hash,
        )

        patch_result = patch_preparer(checkout, facts, ctx) if patch_preparer else None
        if patch_result and patch_result.warnings:
            ctx = replace(
                ctx,
                preparation_warnings=[*ctx.preparation_warnings, *patch_result.warnings][:20],
            )
        if patch_result and patch_result.patch:
            keys = {key.name: key for key in facts.env_keys}
            keys.update({key.name: key for key in patch_result.env_keys})
            changed = {env: dict(tiers) for env, tiers in facts.changed.items()}
            # 패치 결과가 달라지므로 선택 환경의 tier를 보수적으로 재빌드한다.
            for env in changed:
                if facts.target in ("both", env):
                    changed[env] = {tier: True for tier in facts.tiers}
            facts = facts.model_copy(
                update={
                    "env_keys": tuple(keys.values()),
                    "changed": changed,
                    "infra_inputs_changed": facts.infra_inputs_changed
                    or any(k.kind == "secret" for k in patch_result.env_keys),
                }
            )

        def draft(feedback: tuple[str, ...]) -> PlanDraft:
            with _stage("plan", run_id, record_stage), tool_context("generate_plan", run_id):
                return generate_plan(
                    GeneratePlanInput(run_id=run_id, facts=facts, feedback=feedback),
                    ctx,
                    jev_client=jev_client,
                    provider=provider,
                    settings=settings,
                ).draft

        def validate(d: PlanDraft | None) -> Plan:
            with _stage("validate", run_id, record_stage):
                return validate_plan(ValidatePlanInput(run_id=run_id, facts=facts, draft=d), ctx)

        first = draft(())
        try:
            plan = validate(first)
        except DdakToolError as e:
            if e.code is not ErrorCode.PLAN_INVALID:
                raise
            plan = _retry_or_rule(e, strict_ai_check, draft, validate, run_id)
    except BaseException:
        shutil.rmtree(checkout, ignore_errors=True)
        raise
    _log.info(
        "플랜 완료",
        run_id=run_id,
        ms=_ms(t_all),
        fallback=bool(plan.planner and plan.planner.fallback),
    )
    return PlanBundle(
        plan=plan,
        context=ctx,
        facts=facts,
        source=checkout,
        patch=patch_result.patch if patch_result else None,
        patch_meta=patch_result.meta if patch_result else None,
        patch_review=patch_result.review if patch_result else None,
    )


def _retry_or_rule(
    err: DdakToolError,
    strict: bool,
    draft: Callable[[tuple[str, ...]], PlanDraft],
    validate: Callable[[PlanDraft | None], Plan],
    run_id: str,
) -> Plan:
    """엄격 검사일 때만 위반 사유로 재지시 1회. 그래도 안 되면(기본 검사는 즉시) 규칙 계획."""
    if strict:
        _log.warning("계획 초안 불합격, 재지시 1회", run_id=run_id)
        try:
            return validate(draft((err.message,)))
        except DdakToolError as e:
            if e.code is not ErrorCode.PLAN_INVALID:
                raise
    _log.warning("규칙 계획으로 대체", run_id=run_id)
    return validate(None)
