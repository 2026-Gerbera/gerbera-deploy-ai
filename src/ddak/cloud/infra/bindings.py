"""조립부가 주입하는 run별 C1 세션. 자격증명은 컨텍스트·DB·툴 입력에 넣지 않는다."""

from __future__ import annotations

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

from .runtime import CommandRunner, InfraRuntime, SessionKeys


@dataclass(frozen=True)
class InfraBinding:
    runtime: InfraRuntime
    files: Mapping[str, str]
    mode: AdapterMode
    read_session: Callable[[], SessionKeys]
    apply_session: Callable[[], SessionKeys]
    analyzer: Callable[[], Any]


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


def _binding(run_id: str, ctx: RunContext) -> InfraBinding:
    binding = _BINDINGS.get(run_id)
    if (
        binding is None
        or run_id != ctx.run_id
        or binding.mode is not ctx.adapter_mode
        or binding.runtime.settings.project != ctx.project
    ):
        raise DdakToolError(ErrorCode.INFRA_MISSING, "해당 실행의 인프라 세션 연결이 필요하다")
    return binding


def run_validate(inp: ValidateInfraInput, ctx: RunContext) -> ValidateInfraOutput:
    binding = _binding(inp.run_id, ctx)
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
