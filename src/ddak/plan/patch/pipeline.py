"""분석 뒤·계획 전 패치 준비. 생성기는 주입하고 기존 검사기와 원장을 적용한다."""

from __future__ import annotations

import ast
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ddak.core.candidate import strict_patch_scan
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan_facts import EnvKey, Facts, PatchTarget
from ddak.core.patch_ledger import file_diff, guard_patch_loss, reuse_patches
from ddak.core.patch_patterns import scan_patch_targets
from ddak.core.snapshots import apply_diff, copy_source, digest_bytes, file_manifest
from ddak.plan.patch.check import PatchPolicy, check_patch


@dataclass(frozen=True)
class PatchPreparation:
    patch: bytes | None = None
    meta: dict[str, Any] | None = None
    env_keys: tuple[EnvKey, ...] = ()
    changed_files: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


Proposer = Callable[
    [Path, tuple[PatchTarget, ...], RunContext], tuple[bytes, tuple[EnvKey, ...], str]
]


def prepare_patch(
    source: Path,
    facts: Facts,
    ctx: RunContext,
    *,
    previous: Mapping[str, Mapping[str, Any]],
    runs_root: Path,
    proposer: Proposer | None = None,
    scanner: Callable[[Path, bytes], None] | None = None,
) -> PatchPreparation:
    reuse, changed = reuse_patches(source, previous, runs_root)
    reused_files = {
        name
        for release in previous.values()
        for name in release.get("patch_ledger", {})
        if name not in changed
    }
    targets = tuple(
        t for t in facts.patch_targets if t.severity == "patch" and t.file not in reused_files
    )
    proposed, env_keys, origin = None, (), "cache"
    warnings: list[str] = []
    if targets and facts.code_patch:
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
                if patch:
                    remaining = scan_patch_targets(built, ())
                    if any(t.severity == "patch" and t.file in changes for t in remaining):
                        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "개발 설정 잔존")
                    check = check_patch(
                        source, patch, PatchPolicy(allowed_files=frozenset(changes))
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
        guard_patch_loss(source, built, previous)
        if not patch:
            return PatchPreparation(warnings=tuple(warnings))
        # 재사용 패치가 읽는 키도 이번 계획의 주입 대상에 포함한다. 값은 담지 않는다.
        known = {k.name: k for k in env_keys}
        for name in changes:
            tree = ast.parse((built / name).read_text())
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Subscript)
                    and ast.unparse(node.value) == "os.environ"
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)
                ):
                    key = node.slice.value
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
            "new_env_keys": sorted(known),
            "gitleaks": "fixture" if scanner else "gitleaks",
            "patterns": check.patterns,
            "target_hashes": {name: before[name]["sha256"] for name in changes},
        },
        tuple(known.values()),
        changes,
        tuple(warnings),
    )
