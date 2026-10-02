"""AI 설정 패치 검사기(결정적, AI 없음). 담당 장민영(O3). 공통 계약 3-5절.

AI가 만든 unified diff는 신뢰하지 않는 입력이다. 사람 승인 화면에 올리기 전에 코드가 검사한다.
1. 형식: UTF-8, 크기·파일 수 상한, 기존 일반 파일 수정만(새 파일·삭제·이름 변경 금지).
   O1 core.snapshots.apply_diff와 같은 형식만 받는다: hunk 밖의 줄은 ---·+++·@@만
   (diff --git·index 등 Git 확장 헤더는 거부), 파일마다 ---/+++ 쌍 하나와 그 바로 뒤 hunk 하나.
   경로의 ..·절대 경로·따옴표·역슬래시는 거부한다. 이 형식의 diff는 build_patch로 만든다
2. 허용 파일: 정책의 허용 파일(분석이 패턴을 찾은 파일)과 허용 확장자(.py)만
3. 허용 패턴: 지운 줄은 모두 대상 패턴(서명 키 하드코딩, localhost·127.0.0.1 주소, 쿠키 Secure,
   ProxyFix) 줄이어야 한다. 추가한 줄은 대상 패턴 줄, 환경변수를 읽는 줄, import, 괄호·빈 줄·주석만.
   대상 패턴을 지운 파일에는 환경변수를 읽는 줄이 있어야 한다. 위험한 호출과 `;`는 거부한다.
   추가한 줄의 패턴·환경변수 읽기는 주석과 문자열을 뺀 코드에서만 찾는다
   (주석에 패턴 단어만 넣는 우회 방지)
4. 비밀값 리터럴: 추가한 줄에 비밀 이름(SECRET·PASSWORD·TOKEN·API_KEY…)이 있으면 그 줄의 문자열은
   환경변수를 읽는 자리의 키만 허용한다(대문자 값도 거부). 개발값 기본값도 남기지 않는다
5. 적용·문법: O1과 같은 core.snapshots.apply_diff로 임시 사본에 적용하고, 바뀐 .py를 ast로 파싱
6. 적용 후 AST 비교(문자열 이어붙이기·여러 줄 나누기로 줄 검사를 피하는 경우): 원본에 없던 호출은
   허용 목록(환경변수 읽기, ProxyFix, 형 변환)만, 원본에 없던 import는 os·ProxyFix·앱 자체 모듈만
   허용한다.
   비밀 이름에 문자열을 넣는 대입·키워드·dict 항목·환경변수 기본값이 원본보다 늘면 실패
위반 메시지에는 줄 내용을 싣지 않는다(비밀값이 섞일 수 있다). 파일과 줄 번호만 남긴다.
"""

from __future__ import annotations

import ast
import difflib
import re
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
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
# 문자열이 환경변수 키 자리인지: 바로 앞이 os.environ[ / environ.get( / getenv( 등
_ENV_KEY_BEFORE = re.compile(
    r"(?:\benviron\s*\[|\b(?:environ\.get|getenv|require_env|env_bool|env_int)\s*\()\s*$"
)
_STRUCTURAL = re.compile(r"^\s*[()\[\]{},:]*\s*$")
_DANGEROUS = re.compile(
    r"\bos\.system\b|\bsubprocess\b|\beval\s*\(|\bexec\s*\(|__import__|\bopen\s*\("
    r"|\bsocket\b|\burllib\b|\brequests\b|\bhttp\.client\b|;"
    r"|\bgetattr\b|\bsetattr\b|\bglobals\b|\blocals\b|\bvars\s*\(|__builtins__|\bcompile\s*\("
    r"|\bimportlib\b|\bos\.(?:popen|exec\w*|spawn\w*|fork)\b|\bshutil\b|\bpickle\b|\bmarshal\b"
    r"|\bctypes\b|\bpty\b|\bbreakpoint\s*\(",
    re.I,
)
# 줄 수는 ASCII 숫자 7자리까지(유니코드 숫자·아주 긴 숫자로 int()가 예외를 내지 않게)
_HUNK = re.compile(r"^@@ -(\d{1,7})(?:,(\d{1,7}))? \+(\d{1,7})(?:,(\d{1,7}))? @@", re.ASCII)
_HEADER_OK = ("--- ", "+++ ", "@@ ")

# 적용 후 AST 비교에서 원본에 없던 호출로 허용하는 이름. "*."은 호출 결과에 붙은 메서드다
ENV_CALLS = frozenset(
    {"os.environ.get", "environ.get", "os.getenv", "getenv", "require_env", "env_bool", "env_int"}
)
ALLOWED_CALLS = ENV_CALLS | {"ProxyFix", "int", "bool", "str", "float", "*.lower", "*.strip"}
ALLOWED_IMPORTS = frozenset({"os", "werkzeug.middleware.proxy_fix"})


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


def _unsafe_path(name: str) -> bool:
    pure = PurePosixPath(name)
    # 정규화 표기와 다르면 거부: sub//app.py·./app.py로 같은 파일을 두 번 고치지 못하게
    return (
        not name
        or name.startswith(('"', "/"))
        or any(c in name for c in ("\\", "\t", "\r", "\0"))
        or ".." in pure.parts
        or pure.is_absolute()
        or pure.as_posix() != name
    )


def _parse(text: str) -> tuple[list[_FileDiff], list[Violation]]:
    files: list[_FileDiff] = []
    problems: list[Violation] = []
    current: _FileDiff | None = None
    old_name: str | None = None
    pair_ready = False  # ---/+++ 쌍 바로 뒤(다음 줄은 @@여야 한다)
    last_tag: str | None = None  # 바로 앞 줄이 hunk 본문이면 그 태그(-, +, 공백)
    old_no = new_no = old_left = new_left = 0
    for raw in text.split("\n"):
        prev_tag, last_tag = last_tag, None
        if raw.startswith("\\ No newline"):
            # git은 바로 앞 줄의 줄바꿈을 지운다. 그 줄이 자기 쪽(원본/수정본)의 마지막 줄일 때만
            # 받는다. 아니면 다음 문맥 줄이 앞 줄에 붙어 검사기가 본 것과 다르게 적용된다
            ends_side = (
                (prev_tag == "-" and old_left == 0)
                or (prev_tag == "+" and new_left == 0)
                or (prev_tag == " " and old_left == new_left == 0)
            )
            if not ends_side:
                problems.append(Violation("format", message="줄바꿈 없음 표시 위치가 잘못됐다"))
                break
            continue
        if old_left > 0 or new_left > 0:
            tag, body = (raw[:1], raw[1:]) if raw else (" ", "")
            last_tag = tag
            if current is None:
                problems.append(Violation("format", message="파일 헤더 없는 hunk"))
                break
            if tag == "-" and old_left > 0:
                current.removed.append((old_no, body))
                old_no += 1
                old_left -= 1
            elif tag == "+" and new_left > 0:
                current.added.append((new_no, body))
                new_no += 1
                new_left -= 1
            elif tag == " " and old_left > 0 and new_left > 0:
                old_no += 1
                new_no += 1
                old_left -= 1
                new_left -= 1
            else:
                problems.append(Violation("format", current.path, message="hunk 형식 오류"))
                break
            continue
        if raw and not raw.startswith(_HEADER_OK):
            # diff --git·index·모드 변경·이름 변경·바이너리 등. 줄 내용은 싣지 않는다
            problems.append(Violation("scope", message="일반 unified diff 헤더(---/+++/@@)만 허용"))
            continue
        if pair_ready and not raw.startswith("@@ "):
            problems.append(Violation("format", message="---/+++ 쌍 바로 뒤에 hunk가 필요하다"))
            break
        if raw.startswith(("--- ", "+++ ")) and "\t" in raw:
            # 탭 뒤 시각 접미사(1970-01-01 …)를 git은 생성·삭제로 읽는다. build_patch는 안 만든다
            problems.append(Violation("scope", message="파일 이름 뒤 접미사는 허용하지 않는다"))
            break
        if raw.startswith("--- "):
            if old_name is not None:
                problems.append(Violation("format", message="--- 뒤에 +++가 필요하다"))
                break
            old_name = raw[4:]
        elif raw.startswith("+++ "):
            if old_name is None:
                problems.append(Violation("format", message="+++ 앞에 ---가 필요하다"))
                break
            new_name = raw[4:]
            name = new_name if new_name != "/dev/null" else (old_name or "")
            current = _FileDiff(path=name[2:] if name.startswith(("a/", "b/")) else name)
            current.created_or_deleted = "/dev/null" in (old_name, new_name)
            if old_name not in (None, "/dev/null") and old_name[2:] != current.path:
                current.created_or_deleted = True  # 이름 변경
            bad_old = old_name not in (None, "/dev/null") and not old_name.startswith("a/")
            if not name.startswith(("a/", "b/")) or bad_old or _unsafe_path(current.path):
                problems.append(Violation("scope", message="패치 경로가 잘못됐다"))
            if any(f.path == current.path for f in files):
                problems.append(
                    Violation("format", current.path, message="파일마다 hunk 하나만 허용")
                )
            files.append(current)
            old_name, pair_ready = None, True
        elif raw.startswith("@@ "):
            match = _HUNK.match(raw)
            if not match or current is None:
                problems.append(Violation("format", message="hunk 헤더 오류"))
                break
            if not pair_ready:
                # 헤더 하나 아래 두 번째 hunk. O1 apply_diff가 거부한다
                problems.append(
                    Violation("format", current.path, message="파일마다 hunk 하나만 허용")
                )
                break
            pair_ready = False
            old_no, new_no = int(match.group(1)), int(match.group(3))
            old_left = int(match.group(2)) if match.group(2) is not None else 1
            new_left = int(match.group(4)) if match.group(4) is not None else 1
    if old_name is not None or pair_ready:
        problems.append(Violation("format", message="hunk 없는 파일 헤더"))
    if old_left > 0 or new_left > 0:
        problems.append(Violation("format", message="hunk 줄 수가 모자란다"))
    return files, problems


def build_patch(changes: Mapping[str, tuple[str, str]]) -> bytes:
    """{경로: (원본, 수정본)}으로 apply_diff·check_patch 형식의 diff를 만든다.

    파일마다 ---/+++ 쌍과 hunk 하나(파일 전체를 문맥으로)다. 줄바꿈(CRLF/LF)은 원본 그대로 둔다.
    내용이 같은 파일은 넣지 않는다.
    """

    def lines(text: str) -> list[str]:
        # git처럼 LF에서만 나눈다(str.splitlines는 CR·폼피드 등에서도 나눈다)
        return re.findall(r"[^\n]*\n|[^\n]+$", text)

    out: list[str] = []
    for path, (old, new) in sorted(changes.items()):
        if old == new:
            continue
        old_lines, new_lines = lines(old), lines(new)
        context = max(len(old_lines), len(new_lines))
        for line in difflib.unified_diff(
            old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}", n=context
        ):
            out.append(line)
            if not line.endswith("\n"):
                out.append("\n\\ No newline at end of file\n")
    return "".join(out).encode("utf-8")


def _code_only(line: str) -> str:
    """주석을 지우고 문자열 내용을 비운다(따옴표만 남긴다)."""
    out: list[str] = []
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "#":
            break
        if ch in "'\"":
            quote = line[i : i + 3] if line[i : i + 3] in ('"""', "'''") else ch
            end = i + len(quote)
            while end < n and not line.startswith(quote, end):
                end += 2 if line[end] == "\\" else 1
            out.append(quote * 2)
            i = end + len(quote)
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _patterns_in(line: str) -> set[str]:
    return {name for name, regex in PATTERNS.items() if regex.search(line)}


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#") or bool(_STRUCTURAL.match(line))


def _secret_literal(line: str) -> bool:
    """비밀 이름이 있는 줄에 환경변수 키 자리가 아닌 문자열이 있으면 True(대문자 값 포함)."""
    if not _SECRET_NAME.search(line):
        return False
    for match in _STRING.finditer(line):
        key_position = _ENV_KEY_BEFORE.search(line[: match.start()])
        if not (key_position and _ENV_NAME.fullmatch(match.group("s"))):
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
        code = _code_only(line)
        found = _patterns_in(code)
        added |= found
        reads_env = bool(_ENV_READ.search(code))
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


def _existing_file(source: Path, path: str) -> bool:
    target = source / path
    return not target.is_symlink() and target.is_file()


def _syntax(root: Path, paths: Iterable[str]) -> list[Violation]:
    problems = []
    for path in paths:
        if not path.endswith(".py"):
            continue
        try:
            ast.parse((root / path).read_text("utf-8"), filename=path)
        except SyntaxError as error:
            problems.append(Violation("syntax", path, error.lineno, "적용 후 문법 오류"))
        except (UnicodeDecodeError, ValueError, OSError):  # ValueError: NUL 바이트
            problems.append(Violation("format", path, message="UTF-8 파이썬 파일로 읽을 수 없다"))
    return problems


# ---- 적용 후 AST 비교 ----
def _call_name(call: ast.Call) -> str:
    parts: list[str] = []
    func = call.func
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        return ".".join([func.id, *reversed(parts)])
    return f"*.{parts[0]}" if parts else "*"


def _fold_str(node: ast.AST | None) -> str | None:
    """문자열 상수와 그 이어붙이기("SEC" + "RET")를 하나로 접는다. 아니면 None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold_str(node.left), _fold_str(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts = [_fold_str(v) for v in node.values]
        return None if any(p is None for p in parts) else "".join(p or "" for p in parts)
    return None


def _is_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
        isinstance(node, ast.Name) and node.id == "environ"
    )


def _env_default(call: ast.Call) -> ast.AST | None:
    if len(call.args) > 1:
        return call.args[1]
    return next((k.value for k in call.keywords if k.arg == "default"), None)


def _has_literal(node: ast.AST) -> bool:
    """환경변수 키 자리를 뺀 곳에 문자열 값이 있는지."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Call) and _call_name(current) in ENV_CALLS:
            stack += current.args[1:] + [k.value for k in current.keywords]
            stack.append(current.func)
            continue
        if isinstance(current, ast.Subscript) and _is_environ(current.value):
            continue
        if isinstance(current, ast.JoinedStr) or (
            isinstance(current, ast.Constant) and isinstance(current.value, str)
        ):
            return True
        stack += list(ast.iter_child_nodes(current))
    return False


def _target_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _fold_str(node.slice)
    return None


def _is_secret(name: str | None) -> bool:
    return name is not None and bool(_SECRET_NAME.search(name))


def _secret_sites(tree: ast.AST) -> Iterator[ast.AST]:
    """비밀 이름에 문자열 값을 넣는 곳."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if any(_is_secret(_target_name(t)) for t in node.targets) and _has_literal(node.value):
                yield node
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            value = node.value
            if value is not None and _is_secret(_target_name(node.target)) and _has_literal(value):
                yield node
        elif isinstance(node, ast.keyword):
            if _is_secret(node.arg) and _has_literal(node.value):
                yield node
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if _is_secret(_fold_str(key)) and _has_literal(value):
                    yield value
        elif isinstance(node, ast.Call) and _call_name(node) in ENV_CALLS:
            key = _fold_str(node.args[0]) if node.args else None
            default = _env_default(node)
            if (key is None or _is_secret(key)) and default is not None and _has_literal(default):
                yield node


def _imports(tree: ast.AST) -> Iterator[tuple[str, ast.AST]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((alias.name, node) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module, node


def _local_modules(root: Path) -> set[str]:
    return {p.stem if p.suffix == ".py" else p.name for p in root.iterdir()}


def _increased(old: Iterable[str], new: list[tuple[str, ast.AST]]) -> list[tuple[str, ast.AST]]:
    """원본보다 많이 나온 항목(같은 키는 뒤쪽부터 '새로 생긴 것'으로 본다)."""
    budget = Counter(old)
    extra = []
    for key, node in new:
        if budget[key] > 0:
            budget[key] -= 1
        else:
            extra.append((key, node))
    return extra


def _line(node: ast.AST) -> int | None:
    return getattr(node, "lineno", None)


def _ast_diff(source: Path, root: Path, paths: Iterable[str]) -> list[Violation]:
    problems: list[Violation] = []
    local = _local_modules(root)
    for path in paths:
        if not path.endswith(".py"):
            continue
        try:
            old = ast.parse((source / path).read_text("utf-8"), filename=path)
            new = ast.parse((root / path).read_text("utf-8"), filename=path)
        except (SyntaxError, UnicodeDecodeError, ValueError, OSError):
            # 원본이 이미 파싱되지 않으면 비교할 수 없다. 통과시키지 않는다
            problems.append(Violation("syntax", path, message="원본을 파싱할 수 없어 비교 불가"))
            continue

        def calls(tree: ast.AST) -> list[tuple[str, ast.AST]]:
            return [(_call_name(n), n) for n in ast.walk(tree) if isinstance(n, ast.Call)]

        for name, node in _increased((k for k, _ in calls(old)), calls(new)):
            if name not in ALLOWED_CALLS:
                problems.append(
                    Violation("dangerous", path, _line(node), "허용되지 않는 호출이 생겼다")
                )
        for name, node in _increased((k for k, _ in _imports(old)), list(_imports(new))):
            if name not in ALLOWED_IMPORTS and name.split(".")[0] not in local:
                problems.append(
                    Violation("dangerous", path, _line(node), "허용되지 않는 import가 생겼다")
                )
        old_sites = (ast.unparse(n) for n in _secret_sites(old))
        new_sites = [(ast.unparse(n), n) for n in _secret_sites(new)]
        for _, node in _increased(old_sites, new_sites):
            problems.append(
                Violation("secret_literal", path, _line(node), "비밀값 리터럴이 생겼다")
            )
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
        elif not diff.created_or_deleted and not _existing_file(source, diff.path):
            # /dev/null 없이도 git은 없는 파일을 만들 수 있다(old 줄 수 0). 기존 일반 파일만 받는다
            result.violations.append(
                Violation("scope", diff.path, message="기존 일반 파일이 아니다")
            )
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
        if not result.violations:
            result.violations += _ast_diff(source, root, result.files)
    result.passed = not result.violations
    return result
