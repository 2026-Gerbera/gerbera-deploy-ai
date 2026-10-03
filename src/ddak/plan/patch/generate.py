"""patch_config 패치 제안(AI). 담당 장민영(O3). 공통 계약 3-5·3-6절, 10/2 결정 8번.

제품 진입점 propose_intents는 위치 메타데이터만 AI에 전달하고, 코드·값을 받지 않는다.
EditIntent와 reason을 받은 뒤 결정적 render_intents로 diff를 만든다. 검사·승인은 파이프라인 담당.

아래는 호환용 propose_config_patch의 기존 흐름(토글 ctx.toggles["code_patch"]가 켜진 run만):
1. 대상 찾기(코드): 허용 파일에서 패턴과 리터럴 값이 있고 환경변수 읽기가 없는 줄.
   없거나 AI가 빈 edits를 반환하면 no_targets다.
2. 재사용(코드, AI 없음): 이전에 승인된 패치가 새 원본에서도 check_patch를 통과하면 그대로 쓴다.
   build_patch는 파일 전체를 문맥으로 쓰므로, 대상 파일이 한 글자라도 바뀌면 통과하지 못하고
   3으로 간다.
3. 생성(AI): 대상 파일을 줄 번호와 함께 call_ai에 넘기고(비밀값은 관문이 가린다) "줄 범위 → 새 줄"
   수정만 받는다. 코드가 원본에 적용해 build_patch로 diff를 만들고 check_patch로 검사한다.
   불합격이면 위반 코드만 알려 주고 1회 다시 묻는다. 그래도 불합격이면 rejected(예외 아님).
- AI에는 가린 원본만 간다. 줄 번호로 고치므로 가린 값이 실제 파일에 들어가지 않는다.
  가림이 줄 수를 바꾸면(여러 줄 비밀값) 줄 번호가 어긋나므로 AI 없이 rejected다.
- 결과의 patch·meta는 실행기 prepare(patch=, patch_meta=)에 그대로 넘길 수 있는 모양이다
  (meta = reason·reuse·source, executor/approval_meta의 _PatchMeta).
- 툴 등록과 입출력 계약(core/contracts)은 아직 없다(NEEDS_CONTEXT, 정준우).
  그 전에는 이 함수를 쓴다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
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
from ddak.core.env_keys import is_migration_key
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
from ddak.plan.patch.intents import EditIntent, render_intents

PROMPT_VERSION = "patch_config-v1"
MAX_SOURCE_BYTES = 64 * 1024  # 대상 파일 하나의 크기 상한(검사기의 패치 상한과 같다)
MAX_SCAN_FILES = 500
MAX_ATTEMPTS = 2  # 처음 + 위반 코드를 알려 주고 다시 묻기 1회
_SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages"})
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_SCALAR_VALUE = re.compile(r"[=:]\s*(?:-?\d+|True|False)\b")
_EXAMPLE_NAME = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{0,63})\s*=")
_DEFAULT_INTENT_KEYS = frozenset(
    {"SECRET_KEY", "APP_BASE_URL", "SESSION_COOKIE_SECURE", "PROXY_FIX_X_FOR", "PROXY_FIX_X_PROTO"}
)

INSTRUCTION = """\
너는 배포 파이프라인의 설정 패치 작성기다.
아래 파일들은 Flask 앱 원본이며, 각 줄 앞에 줄 번호가 있다.
목표: 환경마다 달라야 하는 값이 코드에 박혀 있는 줄을 환경변수에서 읽도록 고친다.
대상 패턴(이것만 고친다):
- secret_key: 서명 키(SECRET_KEY) 하드코딩 → os.environ["SECRET_KEY"]처럼 환경변수에서 읽기
- local_address: 코드 안 localhost·127.0.0.1 주소 → 환경변수(예: APP_BASE_URL)에서 읽기
- cookie_secure: SESSION_COOKIE_SECURE 고정값 → 환경변수에서 읽어 bool로
- proxy_fix: ProxyFix 신뢰 hop 수 고정값
  → 환경변수(PROXY_FIX_X_FOR, PROXY_FIX_X_PROTO)에서 읽어 int로
규칙:
- edits의 각 항목은 한 파일의 줄 범위 start..end(1부터, 둘 다 포함)를 lines로 바꾼다. 줄을 넣기만
  하려면 end = start - 1. lines에는 줄바꿈 문자를 넣지 않고 원래 들여쓰기를 지킨다.
- 대상 패턴이 있는 줄만 바꾼다. 다른 줄은 고치지 않는다. import는 `import os`만 새로 넣을 수 있다.
- 환경변수 읽기는 os.environ["KEY"], os.environ.get("KEY"), os.getenv("KEY")와 int()/bool()/str()
  변환만 쓴다. 비밀 이름(SECRET·PASSWORD·TOKEN·KEY)에는 기본값 문자열을 두지 않는다.
- 주석을 새로 쓰지 않는다. 문자열 이어 붙이기·별칭 import·세미콜론을 쓰지 않는다.
- [REDACTED]로 가려진 값은 원래 값을 모른다. 그 줄은 통째로 환경변수 읽기로 바꾼다.
- 이미 환경변수에서 읽고 있어 고칠 줄이 없으면 edits를 빈 목록으로 둔다.
- reason: 무엇을 왜 바꿨는지 한국어 200자 이내(비밀값·주소를 쓰지 않는다).
- env_vars: 패치가 새로 읽는 환경변수 이름 목록.
"""

_INTENTS_PROMPT_VERSION = "patch_config-intents-v1"
_INTENTS_INSTRUCTION = """\
너는 배포 설정의 편집 위치만 선택한다. 데이터는 targets와 allowed_keys이며 원문과 값은 없다.
intents와 reason만 JSON으로 반환한다.
- 모든 target에 정확히 하나의 intent를 지정한다. file, line, pattern_id를 그대로 유지한다.
- 각 intent의 필드는 file, line, pattern_id, key뿐이다. 코드·값·diff·줄 내용은 쓰지 않는다.
- key는 반드시 allowed_keys에서 고른다. target.key가 있으면 그대로 쓴다.
  target.key가 없으면 allowed_keys에서 서로 중복되지 않는 이름을 고른다. 새 키를 만들지 않는다.
- 코드가 문자열은 필수 os.environ 읽기, bool은 lower() == 'true', ProxyFix 숫자는 int로 바꾼다.
  변환식이나 기본값은 출력하지 않는다.
- reason은 변경 이유를 한국어 200자 이내로 설명한다. 코드·원문·비밀값·주소는 쓰지 않는다.
"""


class _IntentDraft(BaseModel):
    """새 생성기의 출력 계약. 실행 가능한 편집 내용은 받을 수 없다."""

    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    intents: list[EditIntent] = Field(min_length=1)
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
    status: Literal["proposed", "reused", "no_targets", "rejected"]
    patch: bytes | None = None
    meta: dict[str, object] | None = None  # prepare(patch_meta=)용: reason·reuse·source
    check: PatchCheck | None = None
    targets: dict[str, list[str]] = field(default_factory=dict)  # 경로 → 찾은 패턴
    target_hashes: dict[str, str] = field(default_factory=dict)  # 경로 → 원본 sha256(재사용 기록용)
    env_vars: list[str] = field(default_factory=list)
    attempts: int = 0  # AI 호출 수
    usage: list[AIUsage] = field(default_factory=list)


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


def _clean_env_vars(names: list[str], patch: bytes) -> list[str]:
    added = "\n".join(line for line in patch.decode("utf-8").splitlines() if line.startswith("+"))
    return sorted({n for n in names if _ENV_NAME.match(n) and n in added})


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
        try:
            result = call_ai(
                instruction=_INTENTS_INSTRUCTION + feedback,
                data=data,
                output_model=_IntentDraft,
                prompt_version=_INTENTS_PROMPT_VERSION,
                settings=settings,
                provider=provider,
            )
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
    """source(새 prod 원본 스냅샷)에 맞는 설정 패치를 제안한다. source는 바꾸지 않는다.

    AI를 부르는 경우 tool_context("patch_config", run_id) 안에서 불러야 한다(call_ai 허용 툴 확인).
    """
    if not ctx.toggles.get("code_patch", False):
        raise DdakToolError(ErrorCode.TOGGLE_OFF, "코드 수정 토글이 꺼져 있다")
    _ensure_patch_context()
    base = policy or PatchPolicy()
    targets = find_targets(source, base)
    if not targets:
        return PatchProposal(status="no_targets")
    chosen = dict(sorted(targets.items())[: min(MAX_FILES, base.max_files)])
    originals: dict[str, str] = {}
    for path in chosen:
        text = _read(source, path)
        if text is not None:
            originals[path] = text
    hashes = {p: digest_bytes(t.encode("utf-8")) for p, t in originals.items()}
    check_policy = PatchPolicy(
        allowed_files=frozenset(originals),
        allowed_suffixes=base.allowed_suffixes,
        forbidden_parts=base.forbidden_parts,
        max_files=base.max_files,
    )

    if previous is not None:
        reused = check_patch(source, previous.patch, check_policy)
        if reused.passed:
            return PatchProposal(
                status="reused",
                patch=previous.patch,
                meta={
                    "reason": _checked_reason(reused),
                    "reuse": True,
                    "source": previous.source.value,
                },
                check=reused,
                targets=chosen,
                target_hashes=hashes,
            )

    proposal = PatchProposal(status="rejected", targets=chosen, target_hashes=hashes)
    try:
        data = _numbered(originals, chosen)
    except DdakToolError as exc:
        if exc.code in {ErrorCode.AI_NOT_ALLOWED, ErrorCode.TOGGLE_OFF}:
            raise
        return proposal
    feedback = ""
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
                return proposal
            feedback = "\n이전 응답은 AI_OUTPUT_INVALID로 거부됐다. 출력 계약을 지켜 다시 쓴다."
            continue
        if result.usage is not None:
            proposal.usage.append(result.usage)
        draft = result.value
        if not draft.edits:
            proposal.status, proposal.patch, proposal.check = "no_targets", None, None
            return proposal
        try:
            patch = build_patch(apply_edits(originals, draft.edits))
        except DdakToolError as exc:
            feedback = f"\n이전 제안은 적용할 수 없었다: {exc.message}. 줄 번호를 다시 확인한다."
            continue
        if not patch:
            feedback = "\n이전 제안은 아무것도 바꾸지 않았다. 대상 패턴 줄을 고친다."
            continue
        checked = check_patch(source, patch, check_policy)
        proposal.check = checked
        if checked.passed:
            proposal.status, proposal.patch = "proposed", patch
            proposal.meta = {
                "reason": _checked_reason(checked),
                "reuse": False,
                "source": result.source.value,
            }
            proposal.env_vars = _clean_env_vars(draft.env_vars, patch)
            return proposal
        codes = sorted({v.code for v in checked.violations})
        feedback = (
            f"\n이전 제안은 검사에서 거부됐다(위반 코드: {', '.join(codes)}). "
            "규칙을 지켜 다시 쓴다."
        )
    return proposal
