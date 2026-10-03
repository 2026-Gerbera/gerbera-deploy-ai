"""규칙이 처리하지 않은 로컬 리소스의 표시용 매핑 제안. 생성 계획에는 사용하지 않는다."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from ddak.core.ai.gateway import call_ai
from ddak.core.ai.providers import LLMProvider
from ddak.core.config import Settings
from ddak.core.logging import get_logger
from ddak.core.patch_patterns import (
    DATABASE_SETTINGS,
    environment_reads,
    iter_source_texts,
    scan_patch_targets,
)
from ddak.core.redact import redact

_log = get_logger("plan.analyze")
_TARGETS = ("s3", "rds", "elasticache", "sqs", "ses", "none")
_LOCAL = re.compile(r"(?:localhost|127(?:\.\d{1,3}){3}|\[?::1\]?)", re.I)
_PORTS = {6379: "redis", 5672: "broker", 5671: "broker", 25: "mail", 587: "mail"}
_EXCLUDED_KEYS = DATABASE_SETTINGS | {"IMG_DIR", "SECRET_KEY", "SESSION_COOKIE_SECURE"}
_MAX_EVIDENCE = 12


@dataclass(frozen=True)
class _Evidence:
    evidence_id: str
    kind: str
    file: str
    line: int
    key: str | None


class _Mapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str = Field(max_length=32)
    # JSON 스키마는 허용 목록을 안내하고, 목록 밖 답은 개별적으로 버린다.
    target: str = Field(max_length=32, json_schema_extra={"enum": list(_TARGETS)})
    reason: str = Field(min_length=1, max_length=200)


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mappings: list[_Mapping] = Field(max_length=_MAX_EVIDENCE)


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return node.slice.value if isinstance(node.slice.value, str) else None
    return None


def _setting_names(tree: ast.AST) -> dict[ast.AST, str]:
    names: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        pairs: list[tuple[str | None, ast.AST]] = []
        if isinstance(node, ast.Assign):
            pairs = [(_name(t), node.value) for t in node.targets]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs = [(_name(node.target), node.value)]
        elif isinstance(node, ast.keyword):
            pairs = [(node.arg, node.value)]
        elif isinstance(node, ast.Dict):
            pairs = [
                (k.value, v)
                for k, v in zip(node.keys, node.values, strict=True)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            ]
        for name, value in pairs:
            if name:
                names.update((child, name) for child in ast.walk(value))
    return names


def _kind(value: str) -> str | None:
    if value.startswith("sqlite:"):
        return "sqlite" if not value.endswith(":memory:") else None
    if re.fullmatch(r"[\w./-]+\.(?:sqlite3?|db)", value):
        return "sqlite"
    try:
        address = urlsplit(value if "://" in value else "//" + value)
        if not _LOCAL.fullmatch(address.hostname or ""):
            return None
        scheme_kind = {
            "redis": "redis",
            "rediss": "redis",
            "amqp": "broker",
            "amqps": "broker",
            "smtp": "mail",
            "smtps": "mail",
        }
        return scheme_kind.get(address.scheme) or _PORTS.get(address.port or 0)
    except ValueError:
        return None


def _scan(source: Path) -> list[_Evidence]:
    # 키가 있는 기존 패치 위치는 규칙 경로가 담당한다. 키를 정할 수 없는 리소스 호출은
    # 새 매핑 제안 대상이지만, 패치나 인프라 생성으로 승격하지 않는다.
    handled = {
        (t.file, t.line)
        for t in scan_patch_targets(source, ())
        if t.severity == "patch" and t.key is not None
    }
    found: dict[tuple[str, int, str], str | None] = {}
    for file, text in iter_source_texts(source, python_only=True):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        names = _setting_names(tree)
        ignored = {
            child for read, _ in environment_reads(tree, strict=False) for child in ast.walk(read)
        }
        ignored.update(
            node.body[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            key = names.get(node)
            if node in ignored or key in _EXCLUDED_KEYS or (file, node.lineno) in handled:
                continue
            if kind := _kind(node.value):
                public_key = key if key and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key) else None
                found[(file, node.lineno, kind)] = public_key
    return [
        _Evidence(f"resource-{i}", kind, file, line, key)
        for i, ((file, line, kind), key) in enumerate(sorted(found.items())[:_MAX_EVIDENCE], 1)
    ]


def suggest_infra_mappings(
    source: Path, *, settings: Settings, provider: LLMProvider | None = None
) -> list[dict[str, Any]]:
    """선택 분석. 탐지·요청·출력 처리 실패는 기록하고 빈 제안으로 계속한다."""
    try:
        evidence = _scan(source)
        if not evidence:
            return []
        data = json.dumps([asdict(e) for e in evidence], ensure_ascii=False)
        # 공통 관문의 입력 절단으로 위치 목록이 깨지는 대신 이번 제안만 생략한다.
        if len(data) > 4096:
            raise ValueError("evidence size")
        result = call_ai(
            instruction=(
                "로컬 리소스 흔적에 대응하는 클라우드 서비스 후보를 제안한다. "
                "target은 s3/rds/elasticache/sqs/ses/none 중 하나다. "
                "evidence_id는 입력에 있는 것만 사용하고 reason은 200자 이하로 설명한다. "
                "값이나 코드를 추측하지 않는다. 자동 생성되지 않는 표시용 제안이다."
            ),
            data=data,
            output_model=_Reply,
            settings=replace(settings, ai_retries=0, ai_timeout_s=min(settings.ai_timeout_s, 5)),
            provider=provider,
            prompt_version="infra-mapping-v1",
        )
        by_id = {e.evidence_id: e for e in evidence}
        suggestions = []
        for mapping in result.value.mappings:
            if mapping.target not in _TARGETS or mapping.evidence_id not in by_id:
                _log.warning("리소스 매핑 제안 제외: 허용 목록 또는 근거 불일치")
                continue
            e = by_id.pop(mapping.evidence_id)
            suggestions.append(
                {
                    "kind": e.kind,
                    "file": e.file,
                    "line": e.line,
                    "target": mapping.target,
                    "reason": redact(mapping.reason, max_len=200),
                    "executable": False,
                }
            )
        _log.info("리소스 매핑 제안 완료", evidence_count=len(evidence), count=len(suggestions))
        return suggestions
    except Exception:
        # 공급자/소스 예외 원문은 값이 포함될 수 있으므로 기록하지 않는다.
        _log.warning("리소스 매핑 제안 생략: 분석 또는 응답 처리 실패")
        return []
