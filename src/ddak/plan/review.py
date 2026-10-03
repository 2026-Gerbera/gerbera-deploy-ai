"""선택한 패치의 효과로 재계획하되 prepare의 소스 결합은 원본에 유지한다.

호출자는 생성 측 패치 검사를 마친 뒤, 반환한 bundle과 동일한 patch를 prepare에
함께 넘겨야 한다. 패치·수정본 해시와 최종 승인은 실행기가 별도로 결합한다.
"""

from __future__ import annotations

import ast
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yaml

from ddak.core.config import Settings
from ddak.core.contracts.base import TierName
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_config import DeployConfig
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Plan
from ddak.core.contracts.plan_draft import PlanDraft
from ddak.core.contracts.plan_facts import Env, EnvKey, Facts, FileMeta
from ddak.core.contracts.release import SnapshotBinding
from ddak.core.contracts.tools.analyze_project import AnalyzeProjectInput
from ddak.core.contracts.tools.detect_changed_tiers import DetectChangedTiersInput
from ddak.core.contracts.tools.generate_plan import GeneratePlanInput
from ddak.core.contracts.tools.validate_plan import ValidatePlanInput
from ddak.core.env_keys import is_migration_key
from ddak.core.runtime import tool_context
from ddak.core.snapshots import apply_diff, copy_source, digest_json, file_manifest
from ddak.plan.analyze import analyze_project
from ddak.plan.detect import detect_changed_tiers
from ddak.plan.flow import PlanBundle, _retry_or_rule, _stage
from ddak.plan.planner import generate_plan
from ddak.plan.validate import validate_plan


def _patch_env_keys(source: Path, paths: set[str]) -> set[str]:
    """생성기가 허용하는 환경 키 읽기가 기존 분석에서 누락되면 조용히 진행하지 않는다."""
    readers = {
        "os.getenv",
        "os.environ.get",
        "environ.get",
        "getenv",
        "require_env",
        "env_bool",
        "env_int",
    }
    keys: set[str] = set()
    for path in paths:
        if Path(path).suffix != ".py":
            continue
        try:
            tree = ast.parse((source / path).read_bytes())
        except (SyntaxError, ValueError):
            raise DdakToolError(
                ErrorCode.PLAN_INVALID, "패치 적용본의 Python 문법이 잘못됐다"
            ) from None
        for node in ast.walk(tree):
            key = None
            if isinstance(node, ast.Call) and ast.unparse(node.func) in readers:
                key = node.args[0] if node.args else None
            elif isinstance(node, ast.Subscript) and ast.unparse(node.value) in {
                "os.environ",
                "environ",
            }:
                key = node.slice
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and not is_migration_key(key.value)
            ):
                keys.add(key.value)
    return keys


def replan_patch(
    request: DeployRequest,
    *,
    run_id: str,
    source: Path,
    patch: bytes | None,
    settings: Settings,
    previous_manifests: Callable[[str], Mapping[Env, Mapping[str, FileMeta] | None]],
    previous_tier_manifests: Callable[
        [str], Mapping[Env, Mapping[TierName, Mapping[str, FileMeta]]]
    ]
    | None = None,
    cloud_domain: str | None = None,
    platform: Mapping[str, Any] | None = None,
    provider: Any = None,
    jev_client: Any = None,
    source_context: RunContext | None = None,
    patch_env_keys: tuple[EnvKey, ...] = (),
    record_stage: Callable[[str, int, str], None] | None = None,
) -> PlanBundle:
    """재접수 없이 임시 패치본을 분석한다. 원본과 이전 배포 manifest는 변경하지 않는다.

    facts의 변경 tier·환경 키 등은 패치본의 관측이다. facts_hash와
    source_snapshot_hash는 prepare/facts_reader가 검사할 원본 manifest의 해시다.
    context의 커밋·실행 설정과 새 인프라 승인은 호출자가 채운다.
    previous_tier_manifests는 이월 이미지마다 실제 배포된 files를 제공한다.
    공식 DB artifact 등 코드 비교 대상이 아닌 tier는 호출자가 생략한다.
    원본 previous_manifests는 마이그레이션·DB 초기화 판단에 계속 사용한다.
    """
    if patch is not None and not request.code_patch:
        raise DdakToolError(ErrorCode.TOGGLE_OFF, "코드 수정 토글이 꺼져 있다")
    source = source.expanduser()
    if source.is_symlink():
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "원본 소스는 실제 디렉토리여야 한다")
    source = source.resolve()
    with TemporaryDirectory(prefix="ddak-review-") as temp:
        root = Path(temp)
        candidate = root / "source"
        try:
            original = copy_source(source, candidate)
            original_hash = digest_json(original)
            config = DeployConfig.model_validate(
                yaml.safe_load((candidate / "deploy.yaml").read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, yaml.YAMLError):
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "재검토 원본 소스 또는 deploy.yaml이 올바르지 않다"
            ) from None
        if patch is not None:
            try:
                apply_diff(candidate, patch)
            except (OSError, ValueError, subprocess.SubprocessError):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "선택한 패치를 원본 사본에 적용할 수 없다"
                ) from None
        observed = file_manifest(candidate)
        observed_hash = digest_json(observed)
        ctx = replace(
            source_context or RunContext(run_id, build_backend=settings.build_backend),
            run_id=run_id,
            adapter_mode=settings.adapter_mode,
            deploy_config=config.model_dump(mode="json"),
            project=request.project,
            mode=request.mode,
            toggles={"code_patch": request.code_patch, "strict_ai_check": False},
            cloud_domain=cloud_domain,
            platform=dict(platform or {}),
        )
        previous = previous_manifests(request.project)
        prev: dict[Env, dict[str, FileMeta] | None] = {
            e: None if previous.get(e) is None else dict(previous[e] or {})
            for e in ("local", "cloud")
        }
        with _stage("detect", run_id, record_stage):
            det = detect_changed_tiers(
                DetectChangedTiersInput(
                    run_id=run_id,
                    request=request,
                    source_dir="source",
                    snapshot=SnapshotBinding(
                        source_snapshot_hash=observed_hash, build_snapshot_hash=observed_hash
                    ),
                    previous=prev,
                ),
                ctx,
                root=root,
            )
            if previous_tier_manifests is not None:
                deployed = previous_tier_manifests(request.project)
                changed_paths = set(det.changed_paths)
                for env, tiers in det.changed.items():
                    for tier, tier_config in config.tiers.items():
                        baseline = deployed.get(env, {}).get(tier)
                        if baseline is None:
                            continue
                        # 이월된 이미지의 manifest를 합치면 겹치는 경로의 이력이 사라진다.
                        effective_diff = {
                            path
                            for path in det.manifest.keys() | baseline.keys()
                            if any(
                                prefix in (".", "")
                                or path == prefix
                                or path.startswith(prefix.rstrip("/") + "/")
                                for prefix in tier_config.paths
                            )
                            and det.manifest.get(path) != baseline.get(path)
                        }
                        tiers[tier] |= bool(effective_diff)
                        changed_paths.update(effective_diff)
                det = det.model_copy(update={"changed_paths": tuple(sorted(changed_paths))})
        with _stage("analyze", run_id, record_stage), tool_context("analyze_project", run_id):
            ana = analyze_project(
                AnalyzeProjectInput(
                    run_id=run_id,
                    request=request,
                    source_dir="source",
                    changed=det.changed,
                    new_migrations=det.new_migrations,
                    changed_paths=det.changed_paths,
                ),
                ctx,
                jev_client=jev_client,
                root=root,
            )
        patched_paths = {path for path in observed if observed[path] != original.get(path)}
        required_keys = _patch_env_keys(candidate, patched_paths)
        # 읽기 인식만 확인한다. 추가 주입 여부는 is_new를 사용하는 계획 규칙이 정한다.
        observed_keys = {key.name for key in ana.env_keys}
        if not required_keys <= observed_keys:
            raise DdakToolError(
                ErrorCode.PLAN_INVALID,
                "패치의 환경변수 읽기를 분석기가 모두 인식하지 못했다. "
                "지원되는 읽기 형태가 필요하다",
            )
        keys = {key.name: key for key in ana.env_keys}
        for key in patch_env_keys:
            if key.name in keys:
                keys[key.name] = keys[key.name].model_copy(
                    update={"required": key.required, "kind": key.kind}
                )
            else:
                keys[key.name] = key
        facts = Facts(
            project=request.project,
            mode=request.mode,
            target=request.target,
            tiers=ana.tiers,
            changed=det.changed,
            new_migrations=det.new_migrations,
            modified_migrations=det.modified_migrations,
            env_keys=tuple(keys.values()),
            patch_targets=ana.patch_targets,
            smoke_groups=ana.smoke_groups,
            db_initialized={e: prev[e] is not None for e in prev},
            has_dockerfile=ana.has_dockerfile,
            infra_inputs_changed=ana.infra_inputs_changed,
            code_patch=request.code_patch,
            source_snapshot_hash=original_hash,
            facts_hash=original_hash,
        )

        def draft(feedback: tuple[str, ...]) -> PlanDraft:
            with tool_context("generate_plan", run_id):
                return generate_plan(
                    GeneratePlanInput(run_id=run_id, facts=facts, feedback=feedback),
                    ctx,
                    settings=settings,
                    provider=provider,
                    jev_client=jev_client,
                ).draft

        def validate(d: PlanDraft | None) -> Plan:
            return validate_plan(ValidatePlanInput(run_id=run_id, facts=facts, draft=d), ctx)

        with _stage("plan", run_id, record_stage):
            first = draft(())
        with _stage("validate", run_id, record_stage):
            try:
                plan = validate(first)
            except DdakToolError as exc:
                if exc.code is not ErrorCode.PLAN_INVALID:
                    raise
                plan = _retry_or_rule(exc, False, draft, validate, run_id)
        if digest_json(file_manifest(source)) != original_hash:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "재검토 중 원본 소스가 변경됐다")
        return PlanBundle(plan=plan, context=ctx, facts=facts, source=source)
