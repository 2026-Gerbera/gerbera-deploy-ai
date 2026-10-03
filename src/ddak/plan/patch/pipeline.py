"""분석 뒤·계획 전 패치 준비. 생성기는 주입하고 기존 검사기와 원장을 적용한다."""

from __future__ import annotations

import ast
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ddak.core.candidate import strict_patch_scan
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan_facts import EnvKey, Facts, PatchTarget
from ddak.core.contracts.tools.patch_config import PatchConfigOutput, PatchViolation
from ddak.core.patch_ledger import approved_patches, file_diff, guard_patch_loss, reuse_patches
from ddak.core.patch_patterns import scan_patch_targets
from ddak.core.snapshots import apply_diff, copy_source, digest_bytes, file_manifest
from ddak.plan.patch.check import PatchPolicy, check_patch
from ddak.plan.patch.history import lost_violations, previous_files


@dataclass(frozen=True)
class PatchPreparation:
    patch: bytes | None = None
    meta: dict[str, Any] | None = None
    env_keys: tuple[EnvKey, ...] = ()
    changed_files: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    violations: tuple[PatchViolation, ...] = ()
    review: PatchConfigOutput | None = None

    @classmethod
    def from_output(cls, out: PatchConfigOutput) -> PatchPreparation:
        """등록 툴의 검사 결과를 실행기 승인 입력으로 연결한다."""
        guard_patch_loss(out, run_id=out.run_id, patch=out.patch.encode() if out.patch else None)
        if out.status not in {"proposed", "reused"}:
            if out.passed or out.patch is not None:
                raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "패치 툴 상태가 일치하지 않는다")
            return cls(warnings=tuple(out.warnings), review=out)
        patch = out.patch.encode("utf-8") if out.patch else None
        if (
            out.passed is not True
            or patch is None
            or digest_bytes(patch) != out.patch_sha256
            or out.meta is None
            or out.meta.reuse != (out.status == "reused")
        ):
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "패치 툴 검사 결과가 일치하지 않는다"
            )
        return cls(
            patch=patch,
            meta={
                **out.meta.model_dump(mode="json"),
                "passed": out.passed,
                "patch_sha256": out.patch_sha256,
                "new_env_keys": out.env_vars,
                "target_hashes": out.target_hashes,
            },
            env_keys=tuple(out.env_keys),
            changed_files=tuple(out.changed_files),
            warnings=tuple(out.warnings),
            review=out,
        )


@dataclass(frozen=True)
class PatchSession:
    run_id: str
    source_root: Path
    runs_root: Path
    facts: Facts
    settings: Settings


_SESSION: ContextVar[PatchSession | None] = ContextVar("ddak_patch_session", default=None)


@contextmanager
def patch_session(session: PatchSession) -> Iterator[None]:
    """조립부의 경로·설정을 호출 동안만 주입한다. 비밀 설정은 툴 JSON에 싣지 않는다."""
    token = _SESSION.set(session)
    try:
        yield
    finally:
        _SESSION.reset(token)


def current_patch_session(run_id: str) -> PatchSession | None:
    session = _SESSION.get()
    if session is not None and session.run_id != run_id:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "패치 실행 연결의 run ID가 다르다")
    return session


Proposer = Callable[
    [Path, tuple[PatchTarget, ...], RunContext], tuple[bytes, tuple[EnvKey, ...], str]
]


def _required_env_names(path: Path) -> set[str]:
    return {
        node.slice.value
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Subscript)
        and ast.unparse(node.value) == "os.environ"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    }


def prepare_patch(
    source: Path,
    facts: Facts | None,
    ctx: RunContext,
    *,
    previous: Mapping[str, Mapping[str, Any]],
    runs_root: Path,
    proposer: Proposer | None = None,
    scanner: Callable[[Path, bytes], None] | None = None,
    approved_patch: bytes | None = None,
    policy: PatchPolicy | None = None,
) -> PatchPreparation:
    # core는 승인 diff의 무결성만 확인한다. 파일별 재사용 적합성은 이 툴이 판단한다.
    saved = approved_patches(previous, runs_root)
    exact_reuse, _ = reuse_patches(source, previous, runs_root)
    history = {}
    reusable = {}
    invalid_reuse: dict[str, int | None] = {}
    candidates = previous_files(exact_reuse) if exact_reuse else {}
    for name, data in saved.items():
        parsed = previous_files(data)
        if set(parsed) != {name}:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "승인 diff의 파일 원장이 다르다")
        prev = parsed[name]
        for release in previous.values():
            entry = release.get("patch_ledger", {}).get(name)
            if entry and (
                digest_bytes(prev.old.encode()) != entry["source_sha256"]
                or digest_bytes(prev.new.encode()) != entry["result_sha256"]
            ):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "승인 diff의 원본/결과 해시가 다르다"
                )
        history.update(parsed)
    if approved_patch and not previous:
        history = previous_files(approved_patch)
        manifest = file_manifest(source)
        candidates = {
            name: prev
            for name, prev in history.items()
            if manifest.get(name, {}).get("sha256") == digest_bytes(prev.old.encode())
        }
    for name, prev in candidates.items():
        diff = file_diff(name, prev.old.encode(), prev.new.encode())
        checked = check_patch(source, diff, policy or PatchPolicy(allowed_files=frozenset({name})))
        if checked.passed:
            reusable[name] = diff
        else:
            invalid_reuse[name] = next((v.line for v in checked.violations if v.line), None)
    reuse = b"".join(reusable[name] for name in sorted(reusable)) or None
    reused_files = set(reusable)
    found = facts.patch_targets if facts is not None else scan_patch_targets(source, ())
    targets = tuple(t for t in found if t.severity == "patch" and t.file not in reused_files)
    proposed, env_keys, origin = None, (), "cache"
    warnings: list[str] = []
    enabled = facts.code_patch if facts is not None else ctx.toggles.get("code_patch", False)
    if targets and enabled:
        try:
            if proposer is None:
                raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "생성기 미연결")
            proposed, env_keys, origin = proposer(source, targets, ctx)
        except DdakToolError as exc:
            if exc.code is ErrorCode.AI_NOT_ALLOWED:
                raise
            warnings.append(f"AI 패치 제안 폐기({exc.code.value}); 원본과 성공 원장으로 진행")
            proposed, env_keys, origin = None, (), "cache"
    with tempfile.TemporaryDirectory(prefix="ddak-prepare-patch-") as tmp:
        built = Path(tmp) / "tree"
        # 새 제안 불합격은 폐기한다. 재사용 패치·손실 관문의 오류는 숨기지 않는다.
        for attempt in range(2):
            if attempt:
                import shutil

                shutil.rmtree(built)
            before = copy_source(source, built)
            if reuse:
                apply_diff(built, reuse)
            try:
                if proposed:
                    apply_diff(built, proposed)
                after = file_manifest(built)
                changes = tuple(sorted(name for name in before if before[name] != after.get(name)))
                patch = b"".join(
                    file_diff(name, (source / name).read_bytes(), (built / name).read_bytes())
                    for name in changes
                )
                required_keys: set[str] = set()
                new_keys: set[str] = set()
                if patch:
                    original_keys: set[str] = set()
                    for name in changes:
                        required_keys.update(_required_env_names(built / name))
                        original_keys.update(_required_env_names(source / name))
                    new_keys = required_keys - original_keys
                    if len(new_keys) > 10:  # PatchConfigOutput.env_vars 계약
                        raise DdakToolError(
                            ErrorCode.PRECONDITION_FAILED, "패치의 신규 환경키가 10개를 넘는다"
                        )
                    remaining = scan_patch_targets(built, ())
                    if any(t.severity == "patch" and t.file in changes for t in remaining):
                        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "개발 설정 잔존")
                    check = check_patch(
                        source, patch, policy or PatchPolicy(allowed_files=frozenset(changes))
                    )
                    if not check.passed:
                        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "check_patch 불합격")
                    (scanner or strict_patch_scan)(source, patch)
                break
            except (DdakToolError, ValueError, OSError) as exc:
                if isinstance(exc, DdakToolError) and exc.code is ErrorCode.AI_NOT_ALLOWED:
                    raise
                if not proposed:
                    raise
                code = exc.code.value if isinstance(exc, DdakToolError) else "PATCH_INVALID"
                warnings.append(f"AI 패치 제안 폐기({code}); 원본과 성공 원장으로 진행")
                proposed, env_keys, origin = None, (), "cache"
        final = {
            name: (built / name).read_bytes().decode("utf-8") for name in history if name in after
        }
        violations = list(lost_violations(history, final))
        for name, line in invalid_reuse.items():
            if before.get(name) == after.get(name) and not any(v.file == name for v in violations):
                violations.append(PatchViolation(code="patch_lost", file=name, line=line))
        if violations:
            return PatchPreparation(warnings=tuple(warnings), violations=tuple(violations[:50]))
        if not patch:
            return PatchPreparation(warnings=tuple(warnings))
        # 재사용 패치가 읽는 키도 이번 계획의 주입 대상에 포함한다. 값은 담지 않는다.
        known = {k.name: k for k in env_keys}
        for key in sorted(required_keys):
            known.setdefault(
                key,
                EnvKey(
                    name=key,
                    kind="secret" if "SECRET" in key or "PASSWORD" in key else "plain",
                    reason="approved patch",
                    required=True,
                ),
            )
    return PatchPreparation(
        patch,
        {
            "reason": "개발 설정을 필수 환경변수로 전환",
            "reuse": not bool(proposed),
            "source": origin,
            "passed": True,
            "patch_sha256": digest_bytes(patch),
            "new_env_keys": sorted(new_keys),
            "gitleaks": "fixture" if scanner else "gitleaks",
            "patterns": check.patterns,
            "target_hashes": {name: before[name]["sha256"] for name in changes},
        },
        tuple(known.values()),
        changes,
        tuple(warnings),
    )
