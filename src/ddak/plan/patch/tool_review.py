"""등록 patch_config 내부의 검토 분기. 생성은 기존 intents 경로만 사용한다."""

from __future__ import annotations

from pathlib import Path

from ddak.core.ai.providers import LLMProvider
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.patch_review import CodeProposal
from ddak.core.contracts.plan_facts import Facts
from ddak.core.contracts.tools.patch_config import PatchConfigInput
from ddak.core.patch_ledger import file_diff, reuse_patches
from ddak.core.patch_patterns import scan_patch_targets
from ddak.plan.patch.check import PatchPolicy, check_patch
from ddak.plan.patch.generate import PatchProposal, propose_intents
from ddak.plan.patch.history import previous_files
from ddak.plan.patch.pipeline import PatchPreparation, prepare_patch, required_storage_targets
from ddak.plan.patch.review import combine_review, file_proposals, patch_changes


def _fail(message: str) -> DdakToolError:
    return DdakToolError(ErrorCode.PRECONDITION_FAILED, message)


def prepare_review(
    inp: PatchConfigInput,
    ctx: RunContext,
    *,
    source: Path,
    facts: Facts | None,
    runs_root: Path,
    settings: Settings | None,
    provider: LLMProvider | None,
    trace: PatchProposal,
    policy: PatchPolicy | None = None,
) -> tuple[PatchPreparation, list[CodeProposal]]:
    request = inp.review
    if request is None:
        raise _fail("검토 요청이 없다")
    approved = inp.previous.patch.encode() if inp.previous else None
    reuse, _ = reuse_patches(source, ctx.previous_release, runs_root)
    if approved and not ctx.previous_release:
        reuse = approved
    reused = {
        name: (previous.old, previous.new)
        for name, previous in (previous_files(reuse) if reuse else {}).items()
        if check_patch(
            source,
            file_diff(name, previous.old.encode(), previous.new.encode()),
            policy or PatchPolicy(allowed_files=frozenset({name})),
        ).passed
    }
    required_files = (
        {t.file for t in required_storage_targets(source, ctx)}
        if ctx.toggles.get("code_patch", False)
        else set()
    )

    if request.action == "propose":
        result = prepare_patch(
            source,
            facts,
            ctx,
            previous=ctx.previous_release,
            runs_root=runs_root,
            approved_patch=approved,
            policy=policy,
            proposer=lambda tree, active, context: propose_intents(
                tree, active, context, settings=settings, provider=provider, trace=trace
            ),
            proposal_reason=lambda: (trace.meta or {}).get("reason"),
        )
        proposals = (
            file_proposals(
                source,
                result.patch,
                required_files=set(reused),
                reason=trace.reason or "개발 설정을 필수 환경변수로 전환",
            )
            if result.patch
            else []
        )
        proposals = [
            p.model_copy(update={"required": True}) if p.edits[0].path in required_files else p
            for p in proposals
        ]
        return result, proposals

    proposals = request.proposals
    for name in required_files:
        owners = [p for p in proposals if any(e.path == name for e in p.edits)]
        if len(owners) != 1 or not owners[0].required:
            raise _fail("클라우드 IMG_DIR 필수 제안이 누락되거나 선택 해제됐다")
    # 원장의 필수성은 클라이언트의 required 플래그만 믿지 않고 재확인한다.
    all_patch = combine_review(source, proposals, [p.id for p in proposals])
    all_changes = patch_changes(source, all_patch) if all_patch else {}
    for name, change in reused.items():
        owners = [p for p in proposals if any(e.path == name for e in p.edits)]
        if len(owners) != 1 or not owners[0].required or all_changes.get(name) != change:
            raise _fail("이전 성공 원장의 필수 제안이 누락되거나 변경됐다")

    env_keys = ()
    origin = "cache"
    if request.action == "revise":
        previous = next((p for p in proposals if p.id == request.proposal_id), None)
        if previous is None:
            raise _fail("재검토할 제안이 없다")
        if previous.required:
            raise _fail("이전 성공 원장의 필수 제안은 재수정할 수 없다")
        name = previous.edits[0].path  # combine_review가 파일 단위를 확인했다.
        targets = facts.patch_targets if facts is not None else scan_patch_targets(source, ())
        active = tuple(t for t in targets if t.severity == "patch" and t.file == name)
        if not active:
            raise _fail("재검토할 기존 허용 대상이 없다")
        revised_patch, env_keys, origin = propose_intents(
            source,
            active,
            ctx,
            settings=settings,
            provider=provider,
            trace=trace,
            operator_message=request.prompt,
        )
        if not revised_patch:
            raise _fail("재검토가 빈 결과를 반환했다")
        revised = file_proposals(
            source,
            revised_patch,
            required_files=set(),
            reason=trace.reason or "개발 설정을 필수 환경변수로 전환",
        )
        if len(revised) != 1 or {e.path for e in revised[0].edits} != {name}:
            raise _fail("재검토 범위가 기존 제안 파일과 다르다")
        replacement = revised[0].model_copy(
            update={
                "id": previous.id,
                "revision": previous.revision + 1,
                "requires": previous.requires,
            }
        )
        proposals = [replacement if p.id == previous.id else p for p in proposals]
        selected = [p.id for p in proposals]
    else:
        selected = request.selected

    expected = combine_review(source, proposals, selected)
    changes = patch_changes(source, expected) if expected else {}
    proposed = b"".join(
        file_diff(name, old.encode(), new.encode())
        for name, (old, new) in sorted(changes.items())
        if name not in reused
    )
    if proposed and not ctx.toggles.get("code_patch", False):
        raise DdakToolError(ErrorCode.TOGGLE_OFF, "코드 수정 토글이 꺼져 있다")

    def selected_proposer(tree, active, context):
        if set(changes) - set(reused) - {t.file for t in active}:
            raise _fail("선택된 제안이 기존 허용 대상 밖이다")
        return proposed, env_keys, origin

    result = prepare_patch(
        source,
        facts,
        ctx,
        previous=ctx.previous_release,
        runs_root=runs_root,
        approved_patch=approved,
        policy=policy,
        proposer=selected_proposer,
        proposal_reason=lambda: (
            (trace.meta or {}).get("reason")
            or (
                "이미지 저장 경로를 필수 환경변수로 전환; 규칙으로 보완"
                if any("규칙으로 보완" in p.reason for p in proposals if p.id in selected)
                else None
            )
        ),
    )
    if result.violations:
        return result, []
    if result.patch != expected or result.warnings:
        raise _fail("선택된 패치와 검사 결과가 다르다; 선택 결과를 폐기할 수 없다")
    return result, proposals
