"""조립부가 주입하는 run별 C1 세션. 자격증명은 컨텍스트·DB·툴 입력에 넣지 않는다."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.apply_infra import ApplyInfraInput, ApplyInfraOutput
from ddak.core.contracts.tools.plan_infra import PlanInfraInput, PlanInfraOutput
from ddak.core.contracts.tools.validate_infra import ValidateInfraInput, ValidateInfraOutput
from ddak.core.project_settings import cloud_platform_name

from .runtime import CommandRunner, InfraRuntime, SessionKeys


@dataclass(frozen=True)
class InfraBinding:
    runtime: InfraRuntime
    files: Mapping[str, str]
    mode: AdapterMode
    read_session: Callable[[], SessionKeys]
    apply_session: Callable[[], SessionKeys]
    analyzer: Callable[[], Any]
    generation_source: Source | None = None


_BINDINGS: dict[str, InfraBinding] = {}


def bind_infra(binding: InfraBinding) -> None:
    rid = binding.runtime.run_id
    if rid in _BINDINGS:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "인프라 실행 연결은 교체할 수 없다")
    if binding.mode is AdapterMode.FAKE and isinstance(binding.runtime.runner, CommandRunner):
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, "FAKE 연결에는 fixture 명령 실행기가 필요하다"
        )
    _BINDINGS[rid] = replace(binding, files=dict(binding.files))


def unbind_infra(run_id: str) -> None:
    _BINDINGS.pop(run_id, None)


def has_infra_binding(run_id: str) -> bool:
    """조립부용 존재 조회. 번들 내용·자격증명은 노출하지 않는다."""
    return run_id in _BINDINGS


def _binding(run_id: str, ctx: RunContext) -> InfraBinding:
    binding = _BINDINGS.get(run_id)
    if (
        binding is None
        or run_id != ctx.run_id
        or binding.mode is not ctx.adapter_mode
        or binding.runtime.settings.project
        != cloud_platform_name(ctx.project, ctx.project_settings)
        or binding.runtime.settings.run_project != ctx.project
    ):
        raise DdakToolError(ErrorCode.INFRA_MISSING, "해당 실행의 인프라 세션 연결이 필요하다")
    return binding


def run_validate(inp: ValidateInfraInput, ctx: RunContext) -> ValidateInfraOutput:
    binding = _binding(inp.run_id, ctx)
    if ctx.mode is RunMode.BOOTSTRAP and binding.runtime.settings.layer == "platform":
        if binding.mode is AdapterMode.FAKE and binding.runtime.foundation_clients is None:
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "FAKE 기반 준비에는 fixture SDK가 필요하다"
            )
        binding.runtime.prepare_bootstrap(session=binding.read_session())
    result = binding.runtime.validate(binding.files)
    return ValidateInfraOutput(
        passed=result.passed,
        detail=result.detail,
        source=Source.FIXTURE if binding.mode is AdapterMode.FAKE else Source.LIVE,
    )


def run_plan(inp: PlanInfraInput, ctx: RunContext) -> PlanInfraOutput:
    binding = _binding(inp.run_id, ctx)
    summary = binding.runtime.plan(
        session=binding.read_session(),
        analyzer=binding.analyzer(),
        update=ctx.mode is RunMode.UPDATE,
    )
    if binding.generation_source is not None:
        summary = {
            **summary,
            "headline": summary["headline"] + f" · HCL source={binding.generation_source.value}",
        }
    if len(json.dumps(summary, ensure_ascii=False, sort_keys=True).encode()) > 8192:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, "출처를 포함한 인프라 요약은 8KiB 이하여야 한다"
        )
    return PlanInfraOutput(
        passed=True,
        plan_sha256=summary["plan_sha256"],
        summary=summary,
        source=Source.FIXTURE if binding.mode is AdapterMode.FAKE else Source.LIVE,
    )


def run_apply(inp: ApplyInfraInput, ctx: RunContext) -> ApplyInfraOutput:
    binding = _binding(inp.run_id, ctx)
    if not ctx.lock_token or inp.lock_token != ctx.lock_token:
        raise DdakToolError(ErrorCode.LOCK_INVALID, "인프라 실행 잠금이 다르다")
    result = binding.runtime.apply(session=binding.apply_session())
    return ApplyInfraOutput(
        passed=True,
        layer=binding.runtime.settings.layer,
        **result,
        source=Source.FIXTURE if binding.mode is AdapterMode.FAKE else Source.LIVE,
    )
