"""analyze_project 본체: tier·Dockerfile 유무·환경 키 분류. 규칙이 바닥, Jev는 애매한 키만.

ai_usage는 ask_jev가 사용량을 돌려주지 않으므로 항상 None이다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ddak.core.ai.gateway import ask_jev, get_jev_client
from ddak.core.ai.providers.jev import JevQuestion, JudgmentClient, client_identity
from ddak.core.contracts.base import TierName
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_config import DeployConfig
from ddak.core.contracts.enums import By, RunMode, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan_facts import EnvKey
from ddak.core.contracts.tools.analyze_project import AnalyzeProjectInput, AnalyzeProjectOutput
from ddak.core.env_keys import is_migration_key
from ddak.core.patch_patterns import iter_source_texts, scan_patch_targets
from ddak.core.storage import (
    OUTPUT_KEY,
    STORAGE_ENV_KEY,
    STORAGE_SMOKE_GROUP,
    scan_storage,
    storage_intent,
)
from ddak.plan.analyze import rules

_CONSERVATIVE = "jev 불가: 보수적 secret"


@dataclass
class _Found:
    tier: TierName | None = None
    is_new: bool = False
    snippet: str | None = None  # 키를 쓰는 코드 한 줄(값·기본값 없음)
    required: bool | None = None  # 예시 파일만 있으면 기존 필수 기본값을 유지한다.


@dataclass(frozen=True)
class _Judgment:
    verdict: dict[str, bool] | None
    reason: str
    provider: str | None = None
    model: str | None = None
    source: Source | None = None


def _resolve(source_dir: str, root: Path | None) -> Path:
    if Path(source_dir).is_absolute() or ".." in Path(source_dir).parts:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "source_dir 형식이 올바르지 않다")
    base = root if root is not None else Path(os.environ.get("DDAK_SOURCES_DIR") or "var/sources")
    return base / source_dir


def _parse(ctx: RunContext) -> DeployConfig:
    try:
        return DeployConfig.model_validate(dict(ctx.deploy_config))
    except ValueError:
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED, "deploy_config가 올바르지 않다"
        ) from None


def _collect(
    cfg: DeployConfig, root: Path, changed: frozenset[str], *, bootstrap: bool = False
) -> dict[str, _Found]:
    found: dict[str, _Found] = {}
    if (
        cfg.env_example
        and (root / cfg.env_example).is_file()
        and not any(
            (root / p).is_symlink() for p in (Path(cfg.env_example), *Path(cfg.env_example).parents)
        )
        and not any(p.startswith(".") for p in Path(cfg.env_example).parts[:-1])
    ):
        new = bootstrap or cfg.env_example in changed
        for name in rules.example_keys(root / cfg.env_example):
            f = found.setdefault(name, _Found())
            f.is_new = f.is_new or new
    for name, src, snippet, required in rules.source_key_reads(root, "."):
        f = found.setdefault(name, _Found())
        for tier, tc in cfg.tiers.items():
            if any(Path(src) == Path(rel) or Path(src).is_relative_to(rel) for rel in tc.paths):
                f.tier = f.tier or tier
                break
        f.is_new = f.is_new or src in changed
        f.snippet = f.snippet or snippet
        f.required = required if f.required is None else f.required or required
    return found


def _ask(ambiguous: dict[str, _Found], client: JudgmentClient | None) -> _Judgment:
    """이름 -> secret 여부. Jev 불가·형식 오류면 None(보수 처리). 가드 위반은 그대로 올린다."""
    questions = [
        JevQuestion(
            id=f"secret.{name.lower()}"[:64],
            kind="noul",
            text=(
                f"Is the environment variable {name} a secret credential?"
                + (f" {f.snippet}" if f.snippet else "")
            ),
        )
        for name, f in ambiguous.items()
    ]
    state = "\n".join(
        f"{n}: {f.snippet or '(listed in env example)'}" for n, f in ambiguous.items()
    )
    reason = "jev"
    provider: str | None = None
    model: str | None = None
    try:
        actual = client if client is not None else get_jev_client()
        provider, model = client_identity(actual)
        reason = f"{provider} model={model or 'unknown'}"[:160]
        answers = ask_jev(state=state, questions=questions, client=actual)
    except DdakToolError as exc:
        if exc.code is ErrorCode.AI_NOT_ALLOWED:
            raise
        return _Judgment(None, f"{reason} 불가: 보수적 secret", provider, model)
    prob = {a.id: a.probability for a in answers}
    out: dict[str, bool] = {}
    for q, name in zip(questions, ambiguous, strict=True):
        p = prob.get(q.id)
        if p is None:
            return _Judgment(None, f"{reason} 불가: 보수적 secret", provider, model)
        out[name] = p >= 0.5
    source = getattr(actual, "source", None)
    return _Judgment(
        out, reason, provider, model, source if isinstance(source, Source) else Source.LIVE
    )


def analyze_project(
    inp: AnalyzeProjectInput,
    ctx: RunContext,
    *,
    jev_client: JudgmentClient | None = None,
    root: Path | None = None,
) -> AnalyzeProjectOutput:
    cfg = _parse(ctx)
    src = _resolve(inp.source_dir, root)
    collected = _collect(
        cfg, src, frozenset(inp.changed_paths), bootstrap=ctx.mode is RunMode.BOOTSTRAP
    )
    evidence = scan_storage(dict(iter_source_texts(src, python_only=True)))
    if evidence:
        collected.setdefault(
            STORAGE_ENV_KEY,
            _Found(
                tier="was" if "was" in cfg.tiers else None,
                is_new=ctx.mode is RunMode.BOOTSTRAP
                or any(e.file in inp.changed_paths for e in evidence),
                required=True,
            ),
        )
    intent = storage_intent(bool(evidence), bool(ctx.platform.get("cloud", {}).get(OUTPUT_KEY)))
    found = {name: value for name, value in collected.items() if not is_migration_key(name)}
    verdict: dict[str, rules.Verdict] = {n: rules.classify(n) for n in found}
    ambiguous = {n: found[n] for n, v in verdict.items() if v is None}
    judgment = _ask(ambiguous, jev_client) if ambiguous else _Judgment({}, _CONSERVATIVE)
    jev, ai_reason = judgment.verdict, judgment.reason

    keys: list[EnvKey] = []
    for name in sorted(found):
        kind: Literal["plain", "secret"]
        f, v = found[name], verdict[name]
        if v is not None:
            kind, by, reason = v, By.RULE, "rule: name pattern"
        elif jev is None:
            kind, by, reason = "secret", By.RULE, ai_reason
        else:
            kind, by, reason = ("secret" if jev[name] else "plain"), By.AI, ai_reason
        keys.append(
            EnvKey(
                name=name,
                kind=kind,
                tier=f.tier,
                is_new=f.is_new,
                required=name not in {"RELEASE_ID", "SOURCE_SHA"} and f.required is not False,
                by=by,
                reason=reason,
                source=judgment.source if v is None and jev is not None else None,
                provider=judgment.provider if v is None else None,
                model=judgment.model if v is None else None,
            )
        )
    return AnalyzeProjectOutput(
        tiers=tuple(cfg.tiers),
        env_keys=tuple(keys),
        has_dockerfile={
            t: tc.dockerfile is not None and (src / tc.dockerfile).is_file()
            for t, tc in cfg.tiers.items()
        },
        smoke_groups=(*rules.smoke_groups(cfg, src), *((STORAGE_SMOKE_GROUP,) if evidence else ())),
        infra_inputs_changed=any(k.kind == "secret" and k.is_new for k in keys)
        or (inp.request.target in {"cloud", "both"} and intent is not None),
        patch_targets=scan_patch_targets(
            src, inp.changed_paths, bootstrap=ctx.mode is RunMode.BOOTSTRAP
        ),
        source=judgment.source if ambiguous and jev else None,
    )
