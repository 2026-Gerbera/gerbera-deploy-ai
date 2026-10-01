"""analyze_project 본체: tier·Dockerfile 유무·환경 키 분류. 규칙이 바닥, Jev는 애매한 키만.

ai_usage는 ask_jev가 사용량을 돌려주지 않으므로 항상 None이다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ddak.core.ai.gateway import ask_jev
from ddak.core.ai.providers.jev import JevClient, JevQuestion
from ddak.core.contracts.base import TierName
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_config import DeployConfig
from ddak.core.contracts.enums import By, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan_facts import EnvKey
from ddak.core.contracts.tools.analyze_project import AnalyzeProjectInput, AnalyzeProjectOutput
from ddak.plan.analyze import rules

_CONSERVATIVE = "jev 불가: 보수적 secret"


@dataclass
class _Found:
    tier: TierName | None = None
    is_new: bool = False
    snippet: str | None = None  # 키를 쓰는 코드 한 줄(값·기본값 없음)


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


def _collect(cfg: DeployConfig, root: Path, changed: frozenset[str]) -> dict[str, _Found]:
    found: dict[str, _Found] = {}
    if cfg.env_example and (root / cfg.env_example).is_file():
        new = cfg.env_example in changed
        for name in rules.example_keys(root / cfg.env_example):
            f = found.setdefault(name, _Found())
            f.is_new = f.is_new or new
    for tier, tc in cfg.tiers.items():
        for rel in tc.paths:
            for name, src, snippet in rules.source_keys(root, rel):
                f = found.setdefault(name, _Found())
                f.tier = f.tier or tier
                f.is_new = f.is_new or src in changed
                f.snippet = f.snippet or snippet
    return found


def _ask(ambiguous: dict[str, _Found], client: JevClient | None) -> dict[str, bool] | None:
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
    try:
        answers = ask_jev(state=state, questions=questions, client=client)
    except DdakToolError as exc:
        if exc.code is ErrorCode.AI_NOT_ALLOWED:
            raise
        return None
    prob = {a.id: a.probability for a in answers}
    out: dict[str, bool] = {}
    for q, name in zip(questions, ambiguous, strict=True):
        p = prob.get(q.id)
        if p is None:
            return None
        out[name] = p >= 0.5
    return out


def analyze_project(
    inp: AnalyzeProjectInput,
    ctx: RunContext,
    *,
    jev_client: JevClient | None = None,
    root: Path | None = None,
) -> AnalyzeProjectOutput:
    cfg = _parse(ctx)
    src = _resolve(inp.source_dir, root)
    found = _collect(cfg, src, frozenset(inp.changed_paths))
    verdict = {n: rules.classify(n) for n in found}
    ambiguous = {n: found[n] for n, v in verdict.items() if v is None}
    jev = _ask(ambiguous, jev_client) if ambiguous else {}

    keys: list[EnvKey] = []
    for name in sorted(found):
        f, v = found[name], verdict[name]
        if v is not None:
            kind, by, reason = v, By.RULE, "rule: name pattern"
        elif jev is None:
            kind, by, reason = "secret", By.RULE, _CONSERVATIVE
        else:
            kind, by, reason = ("secret" if jev[name] else "plain"), By.AI, "jev"
        keys.append(
            EnvKey(name=name, kind=kind, tier=f.tier, is_new=f.is_new, by=by, reason=reason)
        )
    return AnalyzeProjectOutput(
        tiers=tuple(cfg.tiers),
        env_keys=tuple(keys),
        has_dockerfile={
            t: tc.dockerfile is not None and (src / tc.dockerfile).is_file()
            for t, tc in cfg.tiers.items()
        },
        infra_inputs_changed=any(k.kind == "secret" and k.is_new for k in keys),
        source=Source.LIVE if ambiguous and jev else None,
    )
