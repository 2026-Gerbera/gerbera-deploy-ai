"""AI 설정 패치 검사기(결정적, AI 없음). 담당 장민영(O3). 공통 계약 3-5절.

AI가 만든 unified diff는 신뢰하지 않는 입력이다. 사람 승인 화면에 올리기 전에 코드가 검사한다.
1. 형식: UTF-8, 크기·파일 수 상한, 기존 일반 파일 수정만(새 파일·삭제·이름 변경 금지)
2. 허용 파일: 정책의 허용 파일(분석이 패턴을 찾은 파일)과 허용 확장자(.py)만
3. 허용 패턴: 지운 줄은 모두 대상 패턴(서명 키 하드코딩, localhost·127.0.0.1 주소, 쿠키 Secure,
   ProxyFix) 줄이어야 한다. 추가한 줄은 대상 패턴 줄, 환경변수를 읽는 줄, import, 괄호·빈 줄·주석만.
   대상 패턴을 지운 파일에는 환경변수를 읽는 줄이 있어야 한다. 위험한 호출과 `;`는 거부한다
4. 비밀값 리터럴: 추가한 줄에 비밀 이름(SECRET·PASSWORD·TOKEN·API_KEY…)과 함께 문자열 값이 있으면
   실패(환경변수 이름 자체는 허용). 개발값 기본값도 남기지 않는다
5. 적용·문법: O1과 같은 core.snapshots.apply_diff로 임시 사본에 적용하고, 바뀐 .py를 ast로 파싱
위반 메시지에는 줄 내용을 싣지 않는다(비밀값이 섞일 수 있다). 파일과 줄 번호만 남긴다.
"""

from __future__ import annotations

import ast
import re
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ddak.core.snapshots import apply_diff, copy_source, digest_bytes

MAX_PATCH_BYTES = 64 * 1024
MAX_FILES = 5

# 대상 패턴(P0). 줄 단위로 본다.
PATTERNS: dict[str, re.Pattern[str]] = {
    "secret_key": re.compile(r"\bSECRET_KEY\b"),
    "local_address": re.compile(r"\blocalhost\b|\b127\.0\.0\.1\b"),
    "cookie_secure": re.compile(r"\bSESSION_COOKIE_SECURE\b"),
    "proxy_fix": re.compile(r"\bProxyFix\b|\bx_for\b|\bx_proto\b|PROXY_FIX_"),
}
_ENV_READ = re.compile(r"\bos\.environ\b|\bgetenv\s*\(|\brequire_env\s*\(|\benv_(?:bool|int)\s*\(")
_IMPORT = re.compile(r"^\s*(?:import\s+[\w.]+(?:\s*,\s*[\w.]+)*|from\s+[\w.]+\s+import\s+.+)\s*$")
_SECRET_NAME = re.compile(r"SECRET|PASSW|PASSWD|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIAL", re.I)
_STRING = re.compile(r"""(?P<q>["'])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)""")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_STRUCTURAL = re.compile(r"^\s*[()\[\]{},:]*\s*$")
_DANGEROUS = re.compile(
    r"\bos\.system\b|\bsubprocess\b|\beval\s*\(|\bexec\s*\(|__import__|\bopen\s*\("
    r"|\bsocket\b|\burllib\b|\brequests\b|\bhttp\.client\b|;"
)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Violation:
    code: str
    file: str = ""
    line: int | None = None
    message: str = ""


@dataclass(frozen=True)
class PatchPolicy:
    """허용 범위.

    allowed_files가 비어 있으면 허용 확장자의 모든 기존 파일(테스트·마이그레이션 제외).
    """

    allowed_files: frozenset[str] = frozenset()
    allowed_suffixes: frozenset[str] = frozenset({".py"})
    forbidden_parts: frozenset[str] = frozenset({"tests", "migrations"})
    max_files: int = MAX_FILES


@dataclass
class PatchCheck:
    passed: bool
    patch_sha256: str | None
    files: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)  # 패치가 다룬 대상 패턴
    violations: list[Violation] = field(default_factory=list)


@dataclass
class _FileDiff:
    path: str
    removed: list[tuple[int, str]] = field(default_factory=list)  # (원본 줄 번호, 내용)
    added: list[tuple[int, str]] = field(default_factory=list)  # (새 줄 번호, 내용)
    created_or_deleted: bool = False


def _parse(text: str) -> tuple[list[_FileDiff], list[Violation]]:
    files: list[_FileDiff] = []
    problems: list[Violation] = []
    current: _FileDiff | None = None
    old_name: str | None = None
    old_no = new_no = old_left = new_left = 0
    for raw in text.split("\n"):
        if old_left > 0 or new_left > 0:
            if raw.startswith("\\ No newline"):
                continue
            tag, body = (raw[:1], raw[1:]) if raw else (" ", "")
            if current is None:
                problems.append(Violation("format", message="파일 헤더 없는 hunk"))
                break
            if tag == "-":
                current.removed.append((old_no, body))
                old_no += 1
                old_left -= 1
            elif tag == "+":
                current.added.append((new_no, body))
                new_no += 1
                new_left -= 1
            elif tag == " ":
                old_no += 1
                new_no += 1
                old_left -= 1
                new_left -= 1
            else:
                problems.append(Violation("format", current.path, message="hunk 형식 오류"))
                break
            continue
        if raw.startswith("--- "):
            old_name = raw[4:].split("\t", 1)[0]
        elif raw.startswith("+++ "):
            new_name = raw[4:].split("\t", 1)[0]
            name = new_name if new_name != "/dev/null" else (old_name or "")
            current = _FileDiff(path=name[2:] if name.startswith(("a/", "b/")) else name)
            current.created_or_deleted = "/dev/null" in (old_name, new_name)
            if old_name not in (None, "/dev/null") and old_name[2:] != current.path:
                current.created_or_deleted = True  # 이름 변경
            files.append(current)
        elif raw.startswith("@@ "):
            match = _HUNK.match(raw)
            if not match or current is None:
                problems.append(Violation("format", message="hunk 헤더 오류"))
                break
            old_no, new_no = int(match.group(1)), int(match.group(3))
            old_left = int(match.group(2)) if match.group(2) is not None else 1
            new_left = int(match.group(4)) if match.group(4) is not None else 1
        elif raw.startswith(
            ("rename ", "copy ", "new file mode", "deleted file mode", "GIT binary")
        ):
            problems.append(Violation("scope", message="기존 텍스트 파일 수정만 허용"))
    return files, problems


def _patterns_in(line: str) -> set[str]:
    return {name for name, regex in PATTERNS.items() if regex.search(line)}


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#") or bool(_STRUCTURAL.match(line))


def _secret_literal(line: str) -> bool:
    if not _SECRET_NAME.search(line):
        return False
    for match in _STRING.finditer(line):
        value = match.group("s")
        if not _ENV_NAME.fullmatch(value):  # 환경변수 이름이 아닌 문자열 값이 같이 있다
            return True
    return False


def _check_lines(diff: _FileDiff) -> tuple[list[Violation], set[str]]:
    problems: list[Violation] = []
    removed: set[str] = set()
    added: set[str] = set()
    env_read = False
    for number, line in diff.removed:
        found = _patterns_in(line)
        removed |= found
        if not found and not _is_blank_or_comment(line):
            problems.append(Violation("pattern", diff.path, number, "대상 패턴이 아닌 줄을 지웠다"))
    for number, line in diff.added:
        found = _patterns_in(line)
        added |= found
        reads_env = bool(_ENV_READ.search(line))
        env_read = env_read or reads_env
        if _secret_literal(line):
            problems.append(Violation("secret_literal", diff.path, number, "비밀값 리터럴이 있다"))
        if _DANGEROUS.search(line):
            problems.append(Violation("dangerous", diff.path, number, "허용되지 않는 호출이 있다"))
        if _is_blank_or_comment(line) or _IMPORT.match(line) or reads_env or found:
            continue
        problems.append(Violation("pattern", diff.path, number, "대상 패턴이 아닌 줄을 추가했다"))
    if removed and not env_read:
        problems.append(Violation("env_read", diff.path, message="환경변수로 읽는 줄이 없다"))
    return problems, removed | added


def _allowed(path: str, policy: PatchPolicy) -> bool:
    pure = PurePosixPath(path)
    if pure.suffix not in policy.allowed_suffixes or policy.forbidden_parts & set(pure.parts):
        return False
    return not policy.allowed_files or path in policy.allowed_files


def _syntax(root: Path, paths: Iterable[str]) -> list[Violation]:
    problems = []
    for path in paths:
        if not path.endswith(".py"):
            continue
        try:
            ast.parse((root / path).read_text("utf-8"), filename=path)
        except SyntaxError as error:
            problems.append(Violation("syntax", path, error.lineno, "적용 후 문법 오류"))
    return problems


def check_patch(source: Path, patch: bytes, policy: PatchPolicy | None = None) -> PatchCheck:
    """source(원본 스냅샷 폴더)에 patch를 적용해도 되는지 검사한다. source는 바꾸지 않는다."""
    policy = policy or PatchPolicy()
    result = PatchCheck(passed=False, patch_sha256=digest_bytes(patch) if patch else None)
    if not patch.strip():
        result.violations.append(Violation("format", message="빈 패치"))
        return result
    if len(patch) > MAX_PATCH_BYTES:
        result.violations.append(Violation("format", message="패치가 너무 크다"))
        return result
    try:
        text = patch.decode("utf-8")
    except UnicodeDecodeError:
        result.violations.append(Violation("format", message="UTF-8이 아니다"))
        return result

    diffs, problems = _parse(text)
    result.violations += problems
    if not diffs:
        result.violations.append(Violation("format", message="수정할 파일이 없다"))
    if len(diffs) > policy.max_files:
        result.violations.append(Violation("scope", message="수정 파일이 너무 많다"))
    touched: set[str] = set()
    for diff in diffs:
        result.files.append(diff.path)
        if diff.created_or_deleted:
            result.violations.append(
                Violation("scope", diff.path, message="새 파일·삭제·이름 변경")
            )
        if not _allowed(diff.path, policy):
            result.violations.append(Violation("scope", diff.path, message="허용 파일이 아니다"))
        line_problems, found = _check_lines(diff)
        result.violations += line_problems
        touched |= found
    result.patterns = sorted(touched)
    if diffs and not touched:
        result.violations.append(Violation("pattern", message="대상 패턴을 다루지 않는다"))
    if result.violations:
        return result

    with tempfile.TemporaryDirectory(prefix="ddak-patch-check-") as temp:
        root = Path(temp) / "source"
        try:
            copy_source(source, root)
            apply_diff(root, patch)
        except (ValueError, OSError, subprocess.SubprocessError) as error:  # apply_diff 고정 문구
            result.violations.append(Violation("apply", message=str(error)[:200]))
            return result
        result.violations += _syntax(root, result.files)
    result.passed = not result.violations
    return result
