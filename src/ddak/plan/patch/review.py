"""파일 단위 제안의 순수 렌더링·검사. AI 호출과 원본 변경은 없다."""

from __future__ import annotations

import ast
import difflib
import tempfile
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.patch_review import CodeProposal, ProposalBatch, ReviewEdit
from ddak.core.patch_ledger import file_diff
from ddak.core.patch_patterns import scan_patch_targets
from ddak.core.redact import redact_python
from ddak.core.snapshots import apply_diff, copy_source, digest_bytes
from ddak.plan.patch.check import ENV_CALLS, check_patch
from ddak.plan.patch.generate import PatchEdit, apply_edits


def _invalid(message: str) -> DdakToolError:
    return DdakToolError(ErrorCode.AI_OUTPUT_INVALID, message)


def _validated(proposals: list[CodeProposal]) -> list[CodeProposal]:
    try:
        return ProposalBatch.model_validate(
            {"proposals": [proposal.model_dump() for proposal in proposals]}
        ).proposals
    except (ValidationError, AttributeError, TypeError):
        raise _invalid("제안 모델 형식 오류") from None


def _closures(proposals: list[CodeProposal]) -> dict[str, set[str]]:
    index = {proposal.id: proposal for proposal in proposals}
    if len(index) != len(proposals):
        raise _invalid("제안 ID가 중복됐다")
    closed: dict[str, set[str]] = {}
    visiting: set[str] = set()

    def visit(key: str) -> set[str]:
        if key not in index:
            raise _invalid("존재하지 않는 의존 제안이다")
        if key in visiting:
            raise _invalid("제안 의존성이 순환한다")
        if key in closed:
            return closed[key]
        dependencies = index[key].requires
        if len(set(dependencies)) != len(dependencies):
            raise _invalid("의존 제안 ID가 중복됐다")
        visiting.add(key)
        result = {key}
        for dependency in dependencies:
            result |= visit(dependency)
        visiting.remove(key)
        closed[key] = result
        return result

    for key in index:
        visit(key)
    return closed


def _env_reads(text: str) -> Counter[str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        raise _invalid("환경변수 분석 대상의 문법 오류") from None
    names: Counter[str] = Counter()
    for node in ast.walk(tree):
        key = None
        if isinstance(node, ast.Subscript) and ast.unparse(node.value) in {"os.environ", "environ"}:
            key = node.slice
        elif isinstance(node, ast.Call) and ast.unparse(node.func) in ENV_CALLS:
            key = node.args[0] if node.args else None
            if key is None:
                raise _invalid("환경변수 이름은 문자열 상수여야 한다")
        if key is not None:
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                raise _invalid("환경변수 이름은 문자열 상수여야 한다")
            names[key.value] += 1
    return names


def patch_changes(source: Path, patch: bytes) -> dict[str, tuple[str, str]]:
    """실제 검사기를 통과한 패치를 파일별 전후 텍스트로 읽는다."""
    checked = check_patch(source, patch)
    if not checked.passed:
        codes = ", ".join(sorted({v.code for v in checked.violations}))
        raise _invalid(f"P0 패치 검사 실패: {codes}")
    with tempfile.TemporaryDirectory(prefix="ddak-review-") as tmp:
        built = Path(tmp) / "tree"
        copy_source(source, built)
        apply_diff(built, patch)
        if any(
            t.severity == "patch" and t.file in checked.files for t in scan_patch_targets(built, ())
        ):
            raise _invalid("제안 파일에 개발 설정이 남았다")
        return {
            name: ((source / name).read_bytes().decode(), (built / name).read_bytes().decode())
            for name in sorted(checked.files)
        }


def _render(source: Path, proposals: list[CodeProposal]) -> bytes:
    targets = {t.file for t in scan_patch_targets(source, ()) if t.severity == "patch"}
    changes: dict[str, tuple[str, str]] = {}
    for proposal in proposals:
        paths = {edit.path for edit in proposal.edits}
        if len(paths) != 1:
            raise _invalid("제안은 공통 import를 포함해 파일 하나로 묶어야 한다")
        name = next(iter(paths))
        if name not in targets or name in changes:
            raise _invalid("제안 파일이 대상 밖이거나 여러 제안으로 나뉘었다")
        text = (source / name).read_bytes().decode()
        edits = [PatchEdit.model_validate(edit.model_dump()) for edit in proposal.edits]
        end = 0
        for edit in sorted(edits, key=lambda e: (e.start, e.end)):
            if edit.start <= end:
                raise _invalid("수정 줄 범위 또는 삽입 위치가 겹친다")
            end = max(edit.start, edit.end)
        change = apply_edits({name: text}, edits)
        old, new = change[name]
        actual = set((_env_reads(new) - _env_reads(old)).keys())
        if len(set(proposal.env_vars)) != len(proposal.env_vars) or actual != set(
            proposal.env_vars
        ):
            raise _invalid("환경변수 메타데이터가 실제 코드와 다르다")
        if old == new:
            raise _invalid("제안이 파일을 변경하지 않는다")
        changes.update(change)
    return b"".join(
        file_diff(name, old.encode(), new.encode()) for name, (old, new) in sorted(changes.items())
    )


def combine_review(
    source: Path, proposals: list[CodeProposal], selected: list[str]
) -> bytes | None:
    """의존·필수 선택과 각 파일을 검사하고 prepare_patch와 같은 바이트로 렌더한다."""
    proposals = _validated(proposals)
    closures = _closures(proposals)
    selection = set(selected)
    if len(selection) != len(selected):
        raise _invalid("선택 ID가 중복됐다")
    if selection - closures.keys():
        raise _invalid("존재하지 않는 제안을 선택했다")
    if any(p.required and p.id not in selection for p in proposals):
        raise _invalid("이전 성공 원장의 필수 제안이 빠졌다")
    if any(closures[key] - selection for key in selected):
        raise _invalid("선택에 필요한 의존 제안이 빠졌다")
    if proposals:
        # 같은 파일의 일부 설정이나 import를 다른 제안으로 보완할 수 없다.
        patch_changes(source, _render(source, proposals))
        for proposal in proposals:
            patch_changes(source, _render(source, [proposal]))
    if not selected:
        return None
    patch = _render(source, [p for p in proposals if p.id in selection])
    patch_changes(source, patch)
    return patch


def proposal_diff(source: Path, proposal: CodeProposal, allproposals: list[CodeProposal]) -> str:
    """표시할 제안과 전이 의존성을 포함한 diff. 다른 파일의 필수 제안은 표시하지 않는다."""
    proposals = _validated(allproposals)
    if not any(p == proposal for p in proposals):
        raise _invalid("목록과 표시할 제안의 ID 또는 내용이 다르다")
    closure = _closures(proposals)[proposal.id]
    patch = combine_review(source, [p for p in proposals if p.id in closure], sorted(closure))
    if not patch:
        return ""
    return "".join(
        "".join(
            difflib.unified_diff(
                redact_python(old).splitlines(keepends=True),
                redact_python(new).splitlines(keepends=True),
                "a/" + name,
                "b/" + name,
                n=3,
            )
        )
        for name, (old, new) in patch_changes(source, patch).items()
    )


def file_proposals(
    source: Path, patch: bytes, *, required_files: set[str], reason: str
) -> list[CodeProposal]:
    """검증된 결과를 파일 단위로 표현한다. hunk별로 선택지를 분리하지 않는다."""
    proposals = []
    try:
        for name, (old, new) in patch_changes(source, patch).items():
            before, after = old.splitlines(), new.splitlines()
            edits = [
                ReviewEdit(path=name, start=i + 1, end=j, lines=after[start:end])
                for tag, i, j, start, end in difflib.SequenceMatcher(
                    a=before, b=after, autojunk=False
                ).get_opcodes()
                if tag != "equal"
            ]
            proposals.append(
                CodeProposal(
                    id="file-" + digest_bytes(name.encode())[7:39],
                    title=("설정 환경변수 전환: " + name)[:100],
                    reason="이전 성공 원장의 승인 패치 재사용"
                    if name in required_files
                    else reason,
                    edits=edits,
                    env_vars=sorted((_env_reads(new) - _env_reads(old)).keys()),
                    required=name in required_files,
                )
            )
    except ValidationError:
        raise _invalid("파일 단위 제안의 크기가 검토 계약을 넘는다") from None
    if combine_review(source, proposals, [p.id for p in proposals]) != patch:
        raise _invalid("검토 제안이 검증된 패치와 일치하지 않는다")
    return proposals
