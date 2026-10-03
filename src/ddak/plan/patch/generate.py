"""patch_config 패치 제안(AI). 담당 장민영(O3). 공통 계약 3-5·3-6절, 10/2 결정 8번.

흐름(10/3 결정 12 반영). 토글 ctx.toggles["code_patch"]가 꺼진 run은 새 AI 제안 없이 2·4만 한다.
꺼져 있고 이전 패치도 없으면 할 일이 없으므로 TOGGLE_OFF로 거부한다.
1. 대상 찾기(코드): 허용 파일(.py, tests·migrations 제외)에서 대상 패턴(PATTERNS)이 있고 값이
   박힌 줄(환경변수 읽기가 없고 값 리터럴이 있는 줄).
2. 파일 단위 재적용(코드, AI 없음, 결정 12의 1): 이전에 승인된 패치를 파일별로 나눈다.
   build_patch는 파일 전체를 문맥으로 쓰므로 hunk에서 그 파일의 이전 원본·수정본을 되살릴 수 있다.
   새 원본이 이전 원본과 바이트까지 같은 파일만 이전 수정본으로 재적용한다(결과 = 이전 수정본).
   지금 검사 규칙을 통과하지 못하면(예: 옛 규칙으로 승인된 기본값 있는 읽기) 토글 ON이면 그 파일도
   3으로 보낸다.
3. 생성(AI, 토글 ON만): 재적용하지 못한 대상 파일만 줄 번호와 함께 call_ai에 넘기고(비밀값은
   관문이 가린다) "줄 범위 → 새 줄" 수정만 받는다. 재적용분과 합쳐 build_patch로 diff를 만들고
   check_patch로 검사한다. 불합격이면 위반 코드만 알려 주고 1회 다시 묻는다. 그래도 불합격이면
   rejected(예외 아님). AI가 고칠 줄이 없다고 답하면(빈 edits) 재적용분만 남는다.
4. 패치 손실 관문(결정 12의 2·7): 이전 패치가 지운 값 줄(대상 패턴이 있는 줄)이 최종 결과에 다시
   나타나면 patch_lost다(passed=False). 호출한 쪽은 승인 전에 run을 멈춘다(결정 12의 6, 실행기 몫).
   이전 패치를 조용히 빼고 배포하지 않는다. 결과가 비면 no_targets, AI 없이 재적용만 했으면 reused,
   AI 제안이 섞이면 proposed다.
- AI에는 가린 원본만 간다. 줄 번호로 고치므로 가린 값이 실제 파일에 들어가지 않는다.
  가림이 줄 수를 바꾸면(여러 줄 비밀값) 줄 번호가 어긋나므로 AI를 부르지 않는다.
- 결과의 patch·meta는 실행기 prepare(patch=, patch_meta=)에 그대로 넘길 수 있는 모양이다
  (meta = reason·reuse·source, executor/approval_meta의 _PatchMeta).
- 툴 진입점은 아래 patch_config(입출력 계약 core/contracts/tools/patch_config.py)다.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ddak.core.ai.gateway import call_ai
from ddak.core.ai.providers import LLMProvider
from ddak.core.config import Settings
from ddak.core.contracts.base import AIUsage
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.patch_config import (
    PatchConfigInput,
    PatchConfigOutput,
    PatchMeta,
    PatchViolation,
)
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

# v2: DB 접속 주소 안내(patch_db_access 흡수), 빈 edits 허용
# v3: 기본값 없는 필수 환경변수 읽기만(결정 12의 4)
PROMPT_VERSION = "patch_config-v3"
MAX_SOURCE_BYTES = 64 * 1024  # 대상 파일 하나의 크기 상한(검사기의 패치 상한과 같다)
MAX_SCAN_FILES = 500
MAX_ATTEMPTS = 2  # 처음 + 위반 코드를 알려 주고 다시 묻기 1회
_SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages"})
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
# = 또는 : 바로 뒤의 숫자·True·False(x_for=1, SESSION_COOKIE_SECURE=False)
_SCALAR_VALUE = re.compile(r"[=:]\s*(?:-?\d+|True|False)\b")

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

    edits: list[PatchEdit] = Field(max_length=20)  # 비면 고칠 줄이 없다는 답
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
    reapplied: list[str] = field(default_factory=list)  # 이전 패치를 그대로 다시 적용한 파일
    lost: list[str] = field(default_factory=list)  # 이전 패치가 지운 값 줄이 다시 나타난 파일


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
    """값이 코드에 박힌 줄인지. 환경변수를 읽는 줄은 이미 고친 것으로 본다.

    키 이름 같은 문자열("SECRET_KEY", "PROXY_FIX_X_FOR")은 값이 아니다.
    """
    if line.lstrip().startswith("#") or _ENV_READ.search(line):
        return False
    if _SCALAR_VALUE.search(line):
        return True
    return any(not _ENV_NAME.match(m.group("s")) for m in _STRING.finditer(line))


def find_targets(source: Path, policy: PatchPolicy | None = None) -> dict[str, list[str]]:
    """경로 → 대상 패턴 이름(정렬). 패턴이 있고 값이 박힌 줄만 센다(문자열 안도 본다)."""
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


# ---- 2. 이전 패치를 파일별로 되살리기, 4. 패치 손실 관문 ---------------------------


@dataclass(frozen=True)
class _PreviousFile:
    old: str  # hunk가 담은 이전 원본(파일 전체 문맥이면 파일 전체)
    new: str  # 이전 수정본
    removed: tuple[str, ...]  # 이전 패치가 지운 줄


def _previous_files(patch: bytes) -> dict[str, _PreviousFile]:
    """build_patch 형식(파일마다 ---/+++와 hunk 하나)의 패치를 파일별 이전 원본·수정본으로 되살린다.

    hunk가 파일 일부만 담았으면 old가 실제 원본과 달라 재적용 대상에서 저절로 빠진다.
    형식이 깨진 패치는 빈 결과(재적용 없음, 손실 판정도 없음)다. 호출한 쪽은 AI로 다시 제안한다.
    """
    try:
        lines = re.findall(r"[^\n]*\n|[^\n]+$", patch.decode("utf-8"))
    except UnicodeDecodeError:
        return {}
    files: dict[str, _PreviousFile] = {}
    i = 0
    while i < len(lines):
        if not (lines[i].startswith("--- ") and i + 2 < len(lines)):
            i += 1
            continue
        target = lines[i + 1].rstrip("\r\n")
        hunk = re.match(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", lines[i + 2])
        if not target.startswith("+++ b/") or hunk is None:
            return {}
        path = target[len("+++ b/") :]
        old_left = int(hunk.group(1)) if hunk.group(1) is not None else 1
        new_left = int(hunk.group(2)) if hunk.group(2) is not None else 1
        old: list[str] = []
        new: list[str] = []
        removed: list[str] = []
        last = ""
        i += 3
        while i < len(lines) and (old_left > 0 or new_left > 0 or lines[i].startswith("\\")):
            tag, body = lines[i][:1], lines[i][1:]
            if tag == "\\":  # "\ No newline at end of file": 바로 앞 줄의 줄바꿈을 지운다
                for side, applies in ((old, last in (" ", "-")), (new, last in (" ", "+"))):
                    if applies and side and side[-1].endswith("\n"):
                        side[-1] = side[-1][:-1]
            elif tag == " " and old_left > 0 and new_left > 0:
                old.append(body)
                new.append(body)
                old_left, new_left = old_left - 1, new_left - 1
            elif tag == "-" and old_left > 0:
                old.append(body)
                removed.append(body.rstrip("\r\n"))
                old_left -= 1
            elif tag == "+" and new_left > 0:
                new.append(body)
                new_left -= 1
            else:
                return {}
            last = tag if tag != "\\" else last
            i += 1
        if old_left or new_left:
            return {}
        files[path] = _PreviousFile("".join(old), "".join(new), tuple(removed))
    return files


def _value_lines(lines: tuple[str, ...]) -> set[str]:
    """대상 패턴이 있는 줄(값이 박혔던 줄)의 공백 정리본."""
    return {
        line.strip()
        for line in lines
        if line.strip() and any(p.search(line) for p in PATTERNS.values())
    }


def _lost_files(previous: Mapping[str, _PreviousFile], final: Mapping[str, str]) -> list[str]:
    """이전 패치가 지운 값 줄이 최종 결과에 다시 나타난 파일(결정 12의 2).

    파일이 없어졌으면 그 값도 없으므로 손실이 아니다.
    """
    lost: list[str] = []
    for path, prev in previous.items():
        text = final.get(path)
        if text is None:
            continue
        present = {line.strip() for line in text.splitlines()}
        if _value_lines(prev.removed) & present:
            lost.append(path)
    return sorted(lost)


# ---- 진입점 -------------------------------------------------------------------------


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
    toggle_on = bool(ctx.toggles.get("code_patch", False))
    if not toggle_on and previous is None:
        raise DdakToolError(
            ErrorCode.TOGGLE_OFF, "코드 수정 토글이 꺼져 있고 유지할 이전 패치도 없다"
        )
    base = policy or PatchPolicy()
    limit = min(MAX_FILES, base.max_files)

    def policy_for(paths: Iterable[str]) -> PatchPolicy:
        return PatchPolicy(
            allowed_files=frozenset(paths),
            allowed_suffixes=base.allowed_suffixes,
            forbidden_parts=base.forbidden_parts,
            max_files=base.max_files,
        )

    targets = find_targets(source, base)
    prev_files = _previous_files(previous.patch) if previous is not None else {}
    current: dict[str, str] = {}
    for path in sorted(set(targets) | set(prev_files)):
        text = _read(source, path)
        if text is not None:
            current[path] = text

    # 2. 파일 단위 재적용: 원본이 이전과 같은 파일만, 결과는 이전 수정본 그대로
    reapplied = dict(
        sorted(
            (path, (current[path], prev.new))
            for path, prev in prev_files.items()
            if current.get(path) == prev.old and prev.old != prev.new
        )[:limit]
    )
    proposal = PatchProposal(status="rejected")
    if reapplied:
        alone = check_patch(source, build_patch(reapplied), policy_for(reapplied))
        if not alone.passed:
            # 옛 규칙으로 승인된 패치(예: 기본값 있는 환경변수 읽기)는 지금 규칙을 넘지 못한다.
            # 토글 ON이면 AI가 다시 제안하고, OFF면 유지할 수 없으므로 아래 손실 관문에서 멈춘다
            reapplied = {}

    # 3. AI 제안: 토글 ON일 때, 재적용하지 못한 대상 파일만
    ai_paths = (
        [p for p in sorted(targets) if p not in reapplied and p in current][
            : max(0, limit - len(reapplied))
        ]
        if toggle_on
        else []
    )
    reported = sorted(set(ai_paths) | set(reapplied))
    proposal.targets = {p: targets[p] for p in reported if p in targets}
    proposal.target_hashes = {p: digest_bytes(current[p].encode("utf-8")) for p in reported}
    proposal.reapplied = sorted(reapplied)
    ai_changes: dict[str, tuple[str, str]] = {}
    draft: PatchDraft | None = None
    ai_source: Source | None = None
    if ai_paths:
        originals = {p: current[p] for p in ai_paths}
        data = _numbered(originals, {p: targets[p] for p in ai_paths})
        feedback = ""
        settled = False
        for _ in range(MAX_ATTEMPTS):
            proposal.attempts += 1
            result = call_ai(
                instruction=INSTRUCTION + feedback,
                data=data,
                output_model=PatchDraft,
                prompt_version=PROMPT_VERSION,
                settings=settings,
                provider=provider,
            )
            if result.usage is not None:
                proposal.usage.append(result.usage)
            proposal.source = result.source
            if not result.value.edits:  # AI가 고칠 줄이 없다고 답했다. 재적용분만 남는다
                settled = True
                break
            try:
                changes = apply_edits(originals, result.value.edits)
            except DdakToolError as exc:
                feedback = (
                    f"\n이전 제안은 적용할 수 없었다: {exc.message}. 줄 번호를 다시 확인한다."
                )
                continue
            combined = {**reapplied, **changes}
            patch = build_patch(combined)
            if not build_patch(changes):
                feedback = "\n이전 제안은 아무것도 바꾸지 않았다. 대상 패턴 줄을 고친다."
                continue
            checked = check_patch(source, patch, policy_for(combined))
            proposal.patch, proposal.check = patch, checked
            if checked.passed:
                ai_changes, draft, ai_source, settled = changes, result.value, result.source, True
                break
            codes = sorted({v.code for v in checked.violations})
            feedback = (
                f"\n이전 제안은 검사에서 거부됐다(위반 코드: {', '.join(codes)}). "
                "규칙을 지켜 다시 쓴다."
            )
        if not settled:
            return proposal  # rejected: 두 번 다 불합격(patch·check는 마지막 제안)

    # 4. 패치 손실 관문: 이전 패치가 지운 값 줄이 최종 결과에 다시 나타나면 멈춘다
    final = {**reapplied, **ai_changes}
    after = {p: (final[p][1] if p in final else current[p]) for p in prev_files if p in current}
    proposal.lost = _lost_files(prev_files, after)
    patch = build_patch(final) if final else None
    if proposal.lost:
        proposal.status, proposal.patch, proposal.check = "patch_lost", patch, None
        return proposal
    if patch is None:
        proposal.status, proposal.patch, proposal.check = "no_targets", None, None
        return proposal
    checked = check_patch(source, patch, policy_for(final))
    proposal.patch, proposal.check = patch, checked
    if not checked.passed:  # 재적용분만으로도 검사를 넘지 못한 경우(위 alone 검사로 거의 없음)
        proposal.status = "rejected"
        return proposal
    if draft is not None and ai_source is not None:
        proposal.status = "proposed"
        proposal.meta = {"reason": draft.reason, "reuse": False, "source": ai_source.value}
        proposal.env_vars = _clean_env_vars(draft.env_vars, patch)
    else:
        if previous is None:  # 재적용분만 남았다면 이전 패치가 있었다
            raise DdakToolError(ErrorCode.INTERNAL, "이전 패치 없이 재적용 결과가 생겼다")
        proposal.status = "reused"
        proposal.meta = {"reason": previous.reason, "reuse": True, "source": previous.source.value}
        proposal.env_vars = _clean_env_vars([], patch)
    return proposal


# ---- 툴 입출력(core/contracts/tools/patch_config.py) ----------------------------------


def _source_root(source_dir: str, root: Path | None) -> Path:
    """analyze_project와 같은 규칙: DDAK_SOURCES_DIR(기본 var/sources) 아래 상대 경로만."""
    if Path(source_dir).is_absolute() or ".." in Path(source_dir).parts:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "source_dir 형식이 올바르지 않다")
    base = root if root is not None else Path(os.environ.get("DDAK_SOURCES_DIR") or "var/sources")
    path = base / source_dir
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
    """툴 진입점. tool_context("patch_config", run_id) 안에서 부른다(계획 흐름이 건다)."""
    previous = None
    if inp.previous is not None:
        previous = PreviousPatch(
            patch=inp.previous.patch.encode("utf-8"),
            reason=inp.previous.reason,
            source=inp.previous.source,
        )
    found = propose_config_patch(
        _source_root(inp.source_dir, root),
        ctx,
        previous=previous,
        policy=policy,
        provider=provider,
        settings=settings,
    )
    check = found.check
    passed = found.status in ("proposed", "reused") and check is not None and check.passed
    return PatchConfigOutput(
        run_id=inp.run_id,
        status=found.status,
        passed=passed,
        patch=found.patch.decode("utf-8") if found.patch else None,
        patch_sha256=check.patch_sha256 if check is not None else None,
        meta=PatchMeta.model_validate(found.meta) if found.meta else None,
        env_vars=found.env_vars,
        targets=found.targets,  # type: ignore[arg-type]  # 이름은 PATTERNS 키
        target_hashes=found.target_hashes,
        violations=[
            *(PatchViolation(code="patch_lost", file=path[:200]) for path in found.lost),
            *(
                PatchViolation(code=v.code[:40], file=v.file[:200], line=v.line)
                for v in (check.violations if check is not None else [])
            ),
        ][:50],
        attempts=found.attempts,
        source=found.source,
        ai_usage=found.usage,
    )
