"""설정 패치 생성과 등록 툴. 담당 장민영(O3), 10/3 결정 12.

제품은 등록된 patch_config → prepare_patch로 검사·성공 원장 재사용을 수행한다.
새 제안은 propose_intents가 위치·허용 키 이름만 AI에 보내고 render_intents가 결정적으로 만든다.
코드·값·diff를 AI에 전달하지 않는다. 토글 OFF는 새 AI 제안 없이 이전 승인 패치만 유지한다.
패치 손실은 patch_lost로 반환하며 호출자가 승인 전에 run을 중단한다.

propose_config_patch는 이전 줄 편집 API 호환용이며 제품에서는 직접 호출하지 않는다.
1. 허용 파일에서 환경변수 읽기가 없는 하드코딩 대상 줄을 찾는다.
2. 이전 승인 패치를 파일별로 나눠 원본 바이트가 같은 파일만 이전 수정본으로 재적용한다.
   재적용분도 현재 check_patch 규칙을 통과해야 한다.
3. 토글 ON만 재적용하지 못한 대상 파일을 AI에 보내고 재적용분과 합쳐 검사한다.
   AI에는 가린 원본만 보내며 가림이 줄 수를 바꾸면 새 제안을 폐기하고 손실부터 판정한다.
   불합격이면 위반 코드로 1회 재시도한다. 실패한 diff·meta는 반환하지 않는다.
4. 이전 패치가 지운 값 줄이 최종 결과에 다시 나타나면 patch_lost다.
   결과가 없으면 no_targets, 재적용만이면 reused, 새 제안이 섞이면 proposed다.
   OFF이고 이전 패치도 없으면 TOGGLE_OFF다. OFF 재적용은 AI 호출 문맥 없이도 가능하다.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ddak.core.ai.gateway import call_ai, ensure_ai_allowed
from ddak.core.ai.providers import LLMProvider
from ddak.core.config import Settings
from ddak.core.contracts.base import AIUsage
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan_facts import EnvKey, PatchTarget
from ddak.core.contracts.tools.patch_config import (
    PatchConfigInput,
    PatchConfigOutput,
    PatchMeta,
    PatchViolation,
)
from ddak.core.env_keys import is_migration_key
from ddak.core.patch_patterns import scan_patch_targets
from ddak.core.redact import redact
from ddak.core.snapshots import digest_bytes
from ddak.plan.patch.check import (
    _ENV_READ,
    _STRING,
    MAX_FILES,
    PATTERNS,
    PatchCheck,
    PatchPolicy,
    build_patch,
    check_patch,
)
from ddak.plan.patch.history import PreviousFile as _PreviousFile
from ddak.plan.patch.history import lost_violations
from ddak.plan.patch.history import previous_files as _previous_files
from ddak.plan.patch.intents import EditIntent, render_intents
from ddak.plan.patch.pipeline import current_patch_session, prepare_patch

# v2: DB 접속 주소 안내(patch_db_access 흡수), 빈 edits 허용
# v3: 기본값 없는 필수 환경변수 읽기만(결정 12의 4)
PROMPT_VERSION = "patch_config-v3"
MAX_SOURCE_BYTES = 64 * 1024  # 대상 파일 하나의 크기 상한(검사기의 패치 상한과 같다)
MAX_SCAN_FILES = 500
MAX_ATTEMPTS = 2  # 처음 + 위반 코드를 알려 주고 다시 묻기 1회
_SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages"})
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_SCALAR_VALUE = re.compile(r"[=:]\s*(?:-?\d+|True|False)\b")
_EXAMPLE_NAME = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{0,63})\s*=")
_DEFAULT_INTENT_KEYS = frozenset(
    {
        "SECRET_KEY",
        "APP_BASE_URL",
        "DATABASE_URL",
        "SESSION_COOKIE_SECURE",
        "PROXY_FIX_X_FOR",
        "PROXY_FIX_X_PROTO",
    }
)

INSTRUCTION = """\
너는 배포 파이프라인의 설정 패치 작성기다.
아래 파일들은 Flask 앱 원본이며, 각 줄 앞에 줄 번호가 있다.
목표: 환경마다 달라야 하는 값이 코드에 박혀 있는 줄을 환경변수에서 읽도록 고친다.
대상 패턴(이것만 고친다):
- secret_key: 서명 키(SECRET_KEY) 하드코딩 → os.environ["SECRET_KEY"]처럼 환경변수에서 읽기
- local_address: 코드 안 localhost·127.0.0.1 주소 → 환경변수에서 읽기
  (앱 주소면 APP_BASE_URL, DB 접속 주소면 DATABASE_URL)
- cookie_secure: SESSION_COOKIE_SECURE 고정값
  → os.environ["SESSION_COOKIE_SECURE"].lower() == "true"처럼 환경변수에서 읽어 bool로
- proxy_fix: ProxyFix 신뢰 hop 수 고정값
  → int(os.environ["PROXY_FIX_X_FOR"])처럼 환경변수(PROXY_FIX_X_FOR, PROXY_FIX_X_PROTO)에서
    읽어 int로
규칙:
- edits의 각 항목은 한 파일의 줄 범위 start..end(1부터, 둘 다 포함)를 lines로 바꾼다. 줄을 넣기만
  하려면 end = start - 1. lines에는 줄바꿈 문자를 넣지 않고 원래 들여쓰기를 지킨다.
- 대상 패턴이 있는 줄만 바꾼다. 다른 줄은 고치지 않는다. import는 `import os`만 새로 넣을 수 있다.
- 환경변수 읽기는 기본값 없는 필수 읽기 os.environ["KEY"]와 int()/str()/.lower() 변환만 쓴다.
  os.environ.get·os.getenv·기본값은 쓰지 않는다(키가 없으면 앱이 바로 실패해야 한다).
  개발용 기본값(dev, localhost 주소, 숫자 등)을 남기지 않는다.
- 주석을 새로 쓰지 않는다. 문자열 이어 붙이기·별칭 import·세미콜론을 쓰지 않는다.
- [REDACTED]로 가려진 값은 원래 값을 모른다. 그 줄은 통째로 환경변수 읽기로 바꾼다.
- 이미 환경변수에서 읽고 있어 고칠 줄이 없으면 edits를 빈 목록으로 둔다.
- reason: 무엇을 왜 바꿨는지 한국어 200자 이내(비밀값·주소를 쓰지 않는다).
- env_vars: 패치가 새로 읽는 환경변수 이름 목록.
"""

_INTENTS_PROMPT_VERSION = "patch_config-intents-v3"
_INTENTS_INSTRUCTION = """\
너는 배포 설정의 편집 위치만 선택한다. 데이터는 targets와 allowed_keys이며 원문과 값은 없다.
intents와 reason만 JSON으로 반환한다.
- 모든 target에 정확히 하나의 intent를 지정한다. file, line, pattern_id를 그대로 유지한다.
- 고칠 대상이 없다고 판단하면 intents를 빈 목록으로 둔다.
- 각 intent의 필드는 file, line, pattern_id, key뿐이다. 코드·값·diff·줄 내용은 쓰지 않는다.
- key는 반드시 allowed_keys에서 고른다. target.key가 있으면 그대로 쓴다.
  target.key가 없으면 allowed_keys에서 서로 중복되지 않는 이름을 고른다. 새 키를 만들지 않는다.
- 코드가 문자열은 필수 os.environ 읽기, bool은 lower() == 'true', ProxyFix 숫자는 int로 바꾼다.
  기본값 없는 필수 읽기만 허용한다. getenv·environ.get·env_bool·env_int 등 선택적 읽기는
  코드 검사에서 env_optional로 거부한다. 변환식이나 기본값은 출력하지 않는다.
- reason은 변경 이유를 한국어 200자 이내로 설명한다. 코드·원문·비밀값·주소는 쓰지 않는다.
"""


class _IntentDraft(BaseModel):
    """새 생성기의 출력 계약. 실행 가능한 편집 내용은 받을 수 없다."""

    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    intents: list[EditIntent]
    reason: str = Field(min_length=1, max_length=200)


class PatchEdit(BaseModel):
    """AI가 돌려주는 수정 하나. 원본 줄 번호 기준."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=200)
    start: int = Field(ge=1, le=100_000)
    end: int = Field(ge=0, le=100_000)  # start - 1이면 넣기만 한다
    lines: list[str] = Field(max_length=40)

    @field_validator("lines")
    @classmethod
    def _single_lines(cls, value: list[str]) -> list[str]:
        if any("\n" in line or "\r" in line or len(line) > 400 for line in value):
            raise ValueError("lines의 각 항목은 한 줄(400자 이하)이어야 한다")
        return value


class PatchDraft(BaseModel):
    """call_ai 출력 모델. 검사 전의 제안이다."""

    model_config = ConfigDict(extra="forbid")

    edits: list[PatchEdit] = Field(max_length=20)  # 빈 목록은 고칠 줄이 없다는 답이다.
    reason: str = Field(min_length=1, max_length=200)
    env_vars: list[str] = Field(default_factory=list, max_length=10)


@dataclass(frozen=True)
class PreviousPatch:
    """이전에 승인된 패치(재사용 후보). 실행기 승인 기록의 patch·patch_meta에서 온다."""

    patch: bytes
    reason: str
    source: Source = Source.LIVE


@dataclass
class PatchProposal:
    status: Literal["proposed", "reused", "no_targets", "rejected", "patch_lost"]
    patch: bytes | None = None
    meta: dict[str, object] | None = None  # prepare(patch_meta=)용: reason·reuse·source
    check: PatchCheck | None = None
    targets: dict[str, list[str]] = field(default_factory=dict)  # 경로 → 찾은 패턴
    target_hashes: dict[str, str] = field(default_factory=dict)  # 경로 → 원본 sha256(재사용 기록용)
    env_vars: list[str] = field(default_factory=list)
    attempts: int = 0  # AI 호출 수
    usage: list[AIUsage] = field(default_factory=list)
    source: Source | None = None  # 마지막 AI 결과의 출처(AI를 안 불렀으면 None)
    reason: str | None = None  # 검토 UI용 실제 모델 이유(가림 후); 승인 메타와 별개다.
    reapplied: list[str] = field(default_factory=list)  # 이전 패치를 그대로 다시 적용한 파일
    lost: list[str] = field(default_factory=list)  # 이전 패치가 지운 값 줄이 다시 나타난 파일
    violations: tuple[PatchViolation, ...] = ()  # 공유 손실 판정의 현재 파일·줄 번호


# ---- 1. 대상 찾기 ----------------------------------------------------------------


def _candidate_files(source: Path, policy: PatchPolicy) -> list[str]:
    found: list[str] = []
    for path in sorted(source.rglob("*.py")):
        rel = path.relative_to(source)
        if _SKIP_DIRS & set(rel.parts) or any(p.startswith(".") for p in rel.parts[:-1]):
            continue
        name = rel.as_posix()
        if policy.forbidden_parts & set(rel.parts):
            continue
        if policy.allowed_files and name not in policy.allowed_files:
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
            continue
        found.append(name)
        if len(found) >= MAX_SCAN_FILES:
            break
    return found


def _read(source: Path, rel: str) -> str | None:
    try:
        with (source / rel).open(encoding="utf-8", newline="") as f:  # 줄바꿈을 그대로 둔다
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _hardcoded(line: str) -> bool:
    """815ea60의 리터럴 판정: 이미 환경변수를 읽는 줄과 문자열 키 이름을 제외한다."""
    if line.lstrip().startswith("#") or _ENV_READ.search(line):
        return False
    if _SCALAR_VALUE.search(line):
        return True
    return any(not _ENV_NAME.match(m.group("s")) for m in _STRING.finditer(line))


def find_targets(source: Path, policy: PatchPolicy | None = None) -> dict[str, list[str]]:
    """경로 → 대상 패턴 이름(정렬). 패턴과 리터럴 값이 있는 줄만 센다."""
    policy = policy or PatchPolicy()
    targets: dict[str, list[str]] = {}
    for rel in _candidate_files(source, policy):
        text = _read(source, rel)
        if text is None:
            continue
        hits = {
            name
            for line in text.splitlines()
            for name, pattern in PATTERNS.items()
            if pattern.search(line) and _hardcoded(line)
        }
        if hits:
            targets[rel] = sorted(hits)
    return targets


# ---- 3. AI 제안을 원본에 적용 --------------------------------------------------------


def _split_lines(text: str) -> list[str]:
    return re.findall(r"[^\n]*\n|[^\n]+$", text)


def _eol(text: str) -> str:
    first = text.find("\n")
    return "\r\n" if first > 0 and text[first - 1] == "\r" else "\n"


def apply_edits(originals: Mapping[str, str], edits: list[PatchEdit]) -> dict[str, tuple[str, str]]:
    """{경로: 원본}에 수정을 적용해 build_patch 입력 {경로: (원본, 수정본)}을 만든다.

    대상이 아닌 파일, 범위 밖, 겹치는 범위는 AI_OUTPUT_INVALID(제안 형식 오류)로 거부한다.
    """
    by_file: dict[str, list[PatchEdit]] = {}
    for edit in edits:
        if edit.path not in originals:
            raise DdakToolError(ErrorCode.AI_OUTPUT_INVALID, "대상이 아닌 파일을 고치려 했다")
        by_file.setdefault(edit.path, []).append(edit)
    changes: dict[str, tuple[str, str]] = {}
    for path, file_edits in by_file.items():
        old = originals[path]
        lines = _split_lines(old)
        eol = _eol(old)
        ordered = sorted(file_edits, key=lambda e: (e.start, e.end))
        last_end = 0
        for edit in ordered:
            if edit.end < edit.start - 1 or edit.end > len(lines) or edit.start > len(lines) + 1:
                raise DdakToolError(ErrorCode.AI_OUTPUT_INVALID, "수정 줄 범위가 파일 밖이다")
            if edit.start <= last_end:
                raise DdakToolError(ErrorCode.AI_OUTPUT_INVALID, "수정 줄 범위가 겹친다")
            last_end = max(last_end, edit.end)
        for edit in reversed(ordered):  # 뒤에서부터 바꿔 앞 줄 번호가 밀리지 않게 한다
            replaced = lines[edit.start - 1 : edit.end]
            keep_no_eol = (
                bool(replaced) and edit.end == len(lines) and not replaced[-1].endswith("\n")
            )
            new = [line + eol for line in edit.lines]
            if keep_no_eol and new:
                new[-1] = new[-1][: -len(eol)]
            lines[edit.start - 1 : edit.end] = new
        changes[path] = (old, "".join(lines))
    return changes


def _numbered(originals: Mapping[str, str], targets: Mapping[str, list[str]]) -> str:
    """AI에 넘길 데이터: 파일마다 대상 패턴과 줄 번호 붙인 본문. 가림이 줄 수를 바꾸면 거부."""
    blocks: list[str] = []
    for path, text in originals.items():
        lines = text.splitlines()
        masked = redact(text).splitlines()
        if len(masked) != len(lines):
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "가림 뒤 줄 수가 달라 줄 번호로 고칠 수 없다"
            )
        body = "\n".join(f"{i:>5}| {line}" for i, line in enumerate(masked, start=1))
        blocks.append(f"### {path} (대상: {', '.join(targets[path])})\n{body}")
    return "\n\n".join(blocks)


_REQUIRED_READ = re.compile(
    r"""(?:\benviron\s*\[\s*|\brequire_env\s*\(\s*)(?P<q>["'])(?P<k>[A-Z][A-Z0-9_]{1,63})(?P=q)"""
)


def _clean_env_vars(names: list[str], patch: bytes) -> list[str]:
    """패치가 추가한 줄에서 실제로 읽는 이름만. AI가 적은 이름 + 추가한 줄의 필수 읽기 키."""
    added = "\n".join(
        line
        for line in patch.decode("utf-8").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    found = {m.group("k") for m in _REQUIRED_READ.finditer(added)}
    return sorted(found | {n for n in names if _ENV_NAME.match(n) and n in added})


# ---- 호환 손실 파일 목록(판정은 공유 history helper가 수행한다) -----------------------


def _lost_files(previous: Mapping[str, _PreviousFile], final: Mapping[str, str]) -> list[str]:
    """이전 API의 파일 목록 모양만 유지한다. 줄 단위 판정은 history와 같다."""
    return sorted({violation.file for violation in lost_violations(previous, final)})


# ---- 진입점 -------------------------------------------------------------------------


def _ensure_patch_context() -> None:
    if ensure_ai_allowed() != "patch_config":
        raise DdakToolError(ErrorCode.AI_NOT_ALLOWED, "patch_config 호출자 문맥이 필요하다")


def _allowed_intent_keys(source: Path, ctx: RunContext) -> frozenset[str]:
    """예시 파일은 키 이름만 추출한다. 마이그레이션 계정은 런타임 허용목록에서 뺀다."""
    names = set(_DEFAULT_INTENT_KEYS)
    example = ctx.deploy_config.get("env_example")
    if example is None:
        return frozenset(names)
    if not isinstance(example, str):
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "환경키 예시 경로 형식이 잘못됐다")
    relative = Path(example)
    if (
        not example
        or relative.is_absolute()
        or relative.as_posix() != example
        or ".." in relative.parts
        or any(c in example for c in "\\\0\r\n")
        or any(p.startswith(".") for p in relative.parts[:-1])
        or (relative.name.startswith(".env") and relative.name != ".env.example")
        or relative.suffix.lower() in {".pem", ".key"}
        or source.is_symlink()
        or any((source / p).is_symlink() for p in (relative, *relative.parents))
    ):
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "허용된 환경키 예시 경로가 아니다")
    path = source / relative
    if not path.is_file():
        return frozenset(names)
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "환경키 예시를 읽을 수 없다") from None
    if len(data) > MAX_SOURCE_BYTES or b"\0" in data:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "환경키 예시 형식 또는 크기가 잘못됐다")
    for line in data.decode("utf-8", errors="replace").splitlines():
        if (match := _EXAMPLE_NAME.match(line)) and not is_migration_key(match.group(1)):
            names.add(match.group(1))
    return frozenset(names)


def _checked_reason(checked: PatchCheck) -> str:
    labels = {
        "secret_key": "서명 키",
        "local_address": "개발 주소",
        "cookie_secure": "쿠키 Secure",
        "proxy_fix": "프록시 hop 수",
    }
    found = [labels[p] for p in sorted(set(checked.patterns)) if p in labels]
    return "환경변수 전환: " + "·".join(found) if found else "환경변수 설정 패치"


def propose_intents(
    source: Path,
    targets: Sequence[PatchTarget],
    ctx: RunContext,
    *,
    settings: Settings | None = None,
    provider: LLMProvider | None = None,
    trace: PatchProposal | None = None,
    operator_message: str | None = None,
) -> tuple[bytes, tuple[EnvKey, ...], str]:
    """위치 의도를 한 번 생성하고, 잘못된 출력만 한 번 재요청한다.

    호출자가 patch_config 툴 문맥을 설정해야 한다. AI 입력은 위치와 허용 키 이름뿐이다.
    반환 str은 AI 출처 라벨이며,
    patch 대상이 없으면 AI 없이 (b'', (), 'rule')을 반환한다.
    """
    if not ctx.toggles.get("code_patch", False):
        raise DdakToolError(ErrorCode.TOGGLE_OFF, "코드 수정 토글이 꺼져 있다")
    _ensure_patch_context()
    try:
        allowed = tuple(PatchTarget.model_validate(t) for t in targets)
    except ValueError:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "패치 대상 계약이 잘못됐다") from None
    active = sorted(
        (t for t in allowed if t.severity == "patch"),
        key=lambda t: (t.file, t.line, t.pattern_id, t.key or ""),
    )
    if not active:
        return b"", (), "rule"
    allowed_keys = _allowed_intent_keys(source, ctx)
    try:
        for target in active:
            EditIntent(
                file=target.file,
                line=target.line,
                pattern_id=target.pattern_id,
                key=target.key if target.key is not None else "PATCH_ENV_KEY",
            )
    except ValueError:
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED, "허용된 편집 위치와 환경키 이름만 전달할 수 있다"
        ) from None
    data = json.dumps(
        {
            "allowed_keys": sorted(allowed_keys),
            "targets": [
                t.model_dump(mode="json", include={"file", "line", "pattern_id", "key"})
                for t in active
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    feedback = ""
    for _ in range(MAX_ATTEMPTS):
        if trace is not None:
            trace.attempts += 1
        try:
            result = call_ai(
                instruction=_INTENTS_INSTRUCTION + feedback,
                data=data,
                output_model=_IntentDraft,
                prompt_version=_INTENTS_PROMPT_VERSION,
                settings=settings,
                provider=provider,
                operator_message=operator_message,
            )
            if trace is not None:
                trace.source = result.source
                reason = result.value.reason
                if settings is not None:
                    secrets = [
                        *settings.provider_keys.values(),
                        settings.anthropic_api_key,
                        settings.groq_api_key,
                        settings.llm_api_key,
                        settings.jev_api_key,
                    ]
                    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
                        reason = reason.replace(secret, "[REDACTED]")
                trace.reason = redact(reason, max_len=200)
                if result.usage is not None:
                    trace.usage.append(result.usage)
            if not result.value.intents:
                return b"", (), result.source.value
            if any(i.key not in allowed_keys for i in result.value.intents):
                raise ValueError("허용목록에 없는 환경키")
            patch, env_keys = render_intents(source, allowed, result.value.intents)
            if not patch:
                raise ValueError("빈 패치 의도")
            return patch, env_keys, result.source.value
        except DdakToolError as exc:
            if exc.code is not ErrorCode.AI_OUTPUT_INVALID:
                raise
        except ValueError:
            pass  # 원본·AI 응답·예외 내용은 재요청 입력으로 보내지 않는다.
        except OSError:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "렌더링할 원본 소스를 읽을 수 없다"
            ) from None
        feedback = (
            "\n이전 응답은 AI_OUTPUT_INVALID로 거부됐다. 허용된 위치와 키를 확인하고, "
            "모든 target에 정확히 하나의 intent를 다시 지정한다."
        )
    raise DdakToolError(ErrorCode.AI_OUTPUT_INVALID, "편집 의도가 두 번 거부됐다")


def propose_config_patch(
    source: Path,
    ctx: RunContext,
    *,
    previous: PreviousPatch | None = None,
    policy: PatchPolicy | None = None,
    provider: LLMProvider | None = None,
    settings: Settings | None = None,
) -> PatchProposal:
    """호환 줄 편집 API. 원본은 바꾸지 않으며 실패한 diff는 반환하지 않는다.

    OFF는 파일별 재적용만 수행한다. 새 제안 실패·빈 응답도 손실을 먼저 판정한다.
    """
    toggle_on = bool(ctx.toggles.get("code_patch", False))
    if not toggle_on and previous is None:
        raise DdakToolError(
            ErrorCode.TOGGLE_OFF, "코드 수정 토글이 꺼져 있고 유지할 이전 패치도 없다"
        )
    if toggle_on:
        _ensure_patch_context()
    base = policy or PatchPolicy()
    limit = min(MAX_FILES, base.max_files)

    def policy_for(paths: Iterable[str]) -> PatchPolicy:
        return PatchPolicy(
            allowed_files=frozenset(paths),
            allowed_suffixes=base.allowed_suffixes,
            forbidden_parts=base.forbidden_parts,
            max_files=base.max_files,
        )

    prev_files = _previous_files(previous.patch) if previous is not None else {}
    targets = find_targets(source, base)
    current: dict[str, str] = {}
    for name in sorted(set(targets) | set(prev_files)):
        relative = Path(name)
        if any((source / part).is_symlink() for part in (relative, *relative.parents)):
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "원본 스냅샷 심볼릭 링크는 허용하지 않는다"
            )
        value = _read(source, name)
        if value is not None:
            current[name] = value
        elif (source / name).exists():
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "이전 패치의 원본을 읽을 수 없다")

    # 한 파일의 옛 env_optional 패치 때문에 다른 정상 파일까지 버리지 않는다.
    reapplied: dict[str, tuple[str, str]] = {}
    for name, prev in sorted(prev_files.items()):
        if base.allowed_files and name not in base.allowed_files:
            continue
        if current.get(name) != prev.old or len(reapplied) >= limit:
            continue
        pair = {name: (current[name], prev.new)}
        checked = check_patch(source, build_patch(pair), policy_for(pair))
        if checked.passed:
            reapplied.update(pair)

    ai_paths = (
        [name for name in sorted(targets) if name not in reapplied and name in current][
            : max(0, limit - len(reapplied))
        ]
        if toggle_on
        else []
    )
    reported = sorted(set(ai_paths) | set(reapplied))
    proposal = PatchProposal(
        status="rejected",
        targets={name: targets[name] for name in reported if name in targets},
        target_hashes={name: digest_bytes(current[name].encode("utf-8")) for name in reported},
        reapplied=sorted(reapplied),
    )
    ai_changes: dict[str, tuple[str, str]] = {}
    draft: PatchDraft | None = None
    ai_source: Source | None = None
    ai_failed = False
    if ai_paths:
        originals = {name: current[name] for name in ai_paths}
        try:
            data = _numbered(originals, {name: targets[name] for name in ai_paths})
        except DdakToolError as exc:
            if exc.code in {ErrorCode.AI_NOT_ALLOWED, ErrorCode.TOGGLE_OFF}:
                raise
            data, ai_failed = "", True
        feedback = ""
        if not ai_failed:
            ai_failed = True
            for _ in range(MAX_ATTEMPTS):
                proposal.attempts += 1
                try:
                    result = call_ai(
                        instruction=INSTRUCTION + feedback,
                        data=data,
                        output_model=PatchDraft,
                        prompt_version=PROMPT_VERSION,
                        settings=settings,
                        provider=provider,
                    )
                except DdakToolError as exc:
                    if exc.code in {ErrorCode.AI_NOT_ALLOWED, ErrorCode.TOGGLE_OFF}:
                        raise
                    if exc.code is not ErrorCode.AI_OUTPUT_INVALID:
                        break
                    feedback = (
                        "\n이전 응답은 AI_OUTPUT_INVALID로 거부됐다. 출력 계약을 지켜 다시 쓴다."
                    )
                    continue
                if result.usage is not None:
                    proposal.usage.append(result.usage)
                proposal.source = result.source
                if not result.value.edits:
                    ai_failed = False
                    break  # 재적용분을 유지한 최종 결과로 손실을 먼저 판정한다.
                try:
                    changes = apply_edits(originals, result.value.edits)
                except DdakToolError as exc:
                    feedback = (
                        f"\n이전 제안은 적용할 수 없었다: {exc.message}. 줄 번호를 다시 확인한다."
                    )
                    continue
                if not build_patch(changes):
                    feedback = "\n이전 제안은 아무것도 바꾸지 않았다. 대상 패턴 줄을 고친다."
                    continue
                combined = {**reapplied, **changes}
                checked = check_patch(source, build_patch(combined), policy_for(combined))
                proposal.check = checked
                if checked.passed:
                    ai_changes, draft, ai_source = changes, result.value, result.source
                    ai_failed = False
                    break
                codes = sorted({v.code for v in checked.violations})
                feedback = (
                    f"\n이전 제안은 검사에서 거부됐다(위반 코드: {', '.join(codes)}). "
                    "규칙을 지켜 다시 쓴다."
                )

    final = {**reapplied, **ai_changes}
    after = {name: (final[name][1] if name in final else value) for name, value in current.items()}
    proposal.violations = lost_violations(prev_files, after)
    proposal.lost = sorted({violation.file for violation in proposal.violations})
    if proposal.lost:
        proposal.status, proposal.check = "patch_lost", None
        return proposal
    patch = build_patch(final) if final else None
    if not patch:
        if not ai_failed:
            proposal.status, proposal.check = "no_targets", None
        return proposal  # rejected도 patch·meta는 None이며 검사 결과만 남긴다.
    checked = check_patch(source, patch, policy_for(final))
    proposal.check = checked
    if not checked.passed:
        # 승인 불가능한 최종 diff는 적용된 결과로 간주하지 않는다.
        proposal.violations = lost_violations(prev_files, current)
        proposal.lost = sorted({violation.file for violation in proposal.violations})
        if proposal.lost:
            proposal.status, proposal.check = "patch_lost", None
        return proposal
    proposal.patch = patch
    if draft is not None and ai_source is not None:
        proposal.status = "proposed"
        proposal.meta = {
            "reason": _checked_reason(checked),
            "reuse": False,
            "source": ai_source.value,
        }
        proposal.env_vars = _clean_env_vars(draft.env_vars, patch)
    else:
        if previous is None:
            raise DdakToolError(ErrorCode.INTERNAL, "이전 패치 없이 재적용 결과가 생겼다")
        proposal.status = "reused"
        proposal.meta = {
            "reason": _checked_reason(checked),
            "reuse": True,
            "source": previous.source.value,
        }
        proposal.env_vars = _clean_env_vars([], patch)
    return proposal


# ---- 툴 입출력(core/contracts/tools/patch_config.py) ----------------------------------


def _source_root(source_dir: str, root: Path | None) -> Path:
    """analyze_project와 같은 규칙: DDAK_SOURCES_DIR(기본 var/sources) 아래 상대 경로만."""
    relative = Path(source_dir)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != source_dir
        or any(c in source_dir for c in "\\\0\r\n")
    ):
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "source_dir 형식이 올바르지 않다")
    base = root if root is not None else Path(os.environ.get("DDAK_SOURCES_DIR") or "var/sources")
    path = base / source_dir
    if base.is_symlink() or any((base / p).is_symlink() for p in (relative, *relative.parents)):
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED, "원본 스냅샷 심볼릭 링크는 허용하지 않는다"
        )
    if not path.is_dir():
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "원본 스냅샷이 없다")
    return path


def patch_config(
    inp: PatchConfigInput,
    ctx: RunContext,
    *,
    root: Path | None = None,
    policy: PatchPolicy | None = None,
    provider: LLMProvider | None = None,
    settings: Settings | None = None,
) -> PatchConfigOutput:
    """등록 툴의 단일 경로: intents → 검사·원장 재사용 → 승인용 출력."""
    if inp.run_id != ctx.run_id:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "패치 요청의 run ID가 다르다")
    session = current_patch_session(ctx.run_id)
    source = _source_root(inp.source_dir, session.source_root if session else root)
    _ensure_patch_context()
    if ctx.previous_release and session is None:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "성공 원장의 실행 경로 연결이 필요하다")
    facts = session.facts if session else None
    if facts is not None and facts.code_patch != ctx.toggles.get("code_patch", False):
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "패치 토글과 분석 결과가 다르다")
    targets = facts.patch_targets if facts else scan_patch_targets(source, ())
    chosen: dict[str, list[str]] = {}
    for target in targets:
        if target.severity == "patch":
            chosen.setdefault(target.file, []).append(target.pattern_id)
    chosen = {name: sorted(set(patterns)) for name, patterns in chosen.items()}
    trace = PatchProposal(status="no_targets")
    proposals = []
    if inp.review is not None:
        from ddak.plan.patch.tool_review import prepare_review

        result, proposals = prepare_review(
            inp,
            ctx,
            source=source,
            facts=facts,
            runs_root=session.runs_root if session else source.parent,
            settings=session.settings if session else settings,
            provider=provider,
            trace=trace,
            policy=policy,
        )
    else:
        result = prepare_patch(
            source,
            facts,
            ctx,
            previous=ctx.previous_release,
            runs_root=session.runs_root if session else source.parent,
            proposer=lambda tree, active, context: propose_intents(
                tree,
                active,
                context,
                settings=session.settings if session else settings,
                provider=provider,
                trace=trace,
            ),
            approved_patch=inp.previous.patch.encode("utf-8") if inp.previous else None,
            policy=policy,
        )
    if result.violations:
        return PatchConfigOutput(
            run_id=inp.run_id,
            status="patch_lost",
            passed=False,
            patch=None,
            meta=None,
            targets=chosen,  # type: ignore[arg-type]
            target_hashes={name: digest_bytes((source / name).read_bytes()) for name in chosen},
            violations=list(result.violations)[:50],
            attempts=trace.attempts,
            source=trace.source,
            ai_usage=trace.usage,
            warnings=list(result.warnings),
        )
    meta = result.meta or {}
    return PatchConfigOutput(
        run_id=inp.run_id,
        status=("reused" if meta.get("reuse") else "proposed")
        if result.patch
        else ("rejected" if result.warnings else "no_targets"),
        passed=bool(result.patch) and meta.get("passed") is True,
        patch=result.patch.decode("utf-8") if result.patch else None,
        patch_sha256=meta.get("patch_sha256"),
        meta=PatchMeta.model_validate(
            {key: value for key, value in meta.items() if key in PatchMeta.model_fields}
        )
        if result.patch
        else None,
        env_vars=meta.get("new_env_keys", []),
        targets=chosen,  # type: ignore[arg-type]
        target_hashes={name: digest_bytes((source / name).read_bytes()) for name in chosen},
        violations=[PatchViolation(code="PATCH_REJECTED")] if result.warnings else [],
        attempts=trace.attempts,
        source=trace.source,
        ai_usage=trace.usage,
        env_keys=list(result.env_keys),
        changed_files=list(result.changed_files),
        warnings=list(result.warnings),
        proposals=proposals,
    )
