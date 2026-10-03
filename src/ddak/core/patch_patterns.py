"""분석과 패치 검사기가 공유하는 P0 패턴. 탐지 결과에는 소스 값을 담지 않는다."""

from __future__ import annotations

import ast
import ipaddress
import os
import re
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from ddak.core.contracts.plan_facts import PatchTarget
from ddak.core.pem import UnsupportedPemError
from ddak.core.snapshots import excluded
from ddak.core.storage import STORAGE_ENV_KEY

__all__ = [
    "DATABASE_SETTINGS",
    "PATTERNS",
    "address_env_key",
    "environment_reads",
    "iter_source_texts",
    "scan_patch_targets",
]

MAX_SCAN_BYTES = 1024 * 1024
DATABASE_SETTINGS = frozenset(
    {"DATABASE", "DATABASE_URL", "SQLALCHEMY_DATABASE_URI", "DB_URL", "DB_URI"}
)

# 검사기의 줄 규칙과 분석기의 pattern_id는 이 레지스트리만 사용한다.
PATTERNS: dict[str, re.Pattern[str]] = {
    "local_storage_dir": re.compile(r"\bIMG_DIR\b"),
    "secret_key": re.compile(r"\bsecret_key\b", re.I),
    "local_address": re.compile(r"\blocalhost\b|\b127(?:\.\d{1,3}){3}\b|::1", re.I),
    "cookie_secure": re.compile(r"\bSESSION_COOKIE_SECURE\b"),
    "proxy_fix": re.compile(r"\bProxyFix\b|\bx_for\b|\bx_proto\b|PROXY_FIX_"),
}
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_WARNING_PARTS = frozenset({"test", "tests", "migration", "migrations"})
_TEXT_ADDRESS = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+"
    r"|\b(?:\d{1,3}\.){3}\d{1,3}\b|[0-9a-fA-F]*:[0-9a-fA-F:]+"
)


def iter_source_texts(root: Path, *, python_only: bool = False) -> Iterator[tuple[str, str]]:
    """manifest 경로 정책으로 순회하고 최대 1MiB+1만 읽는다. 큰 Python은 탐지 불가로 거부한다."""
    if not root.is_dir() or root.is_symlink():
        raise ValueError("소스는 실제 디렉토리여야 한다")
    for directory, dirs, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(".") and not excluded((parent / d).relative_to(root))
        )
        for name in [*dirs, *sorted(names)]:
            path = parent / name
            relative = path.relative_to(root)
            if path.suffix.lower() == ".key" and not path.is_dir():
                raise UnsupportedPemError(relative)
            if excluded(relative) or any(p.startswith(".") for p in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError("소스 심볼릭 링크는 지원하지 않는다")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError("소스에 일반 파일이 아닌 항목이 있다")
            if path.suffix.lower() == ".pem" or (python_only and path.suffix != ".py"):
                continue
            if path.stat().st_size > MAX_SCAN_BYTES:
                if path.suffix == ".py":
                    raise ValueError(f"Python 탐지 크기 상한 초과: {relative.as_posix()}")
                continue
            with path.open("rb") as stream:
                data = stream.read(MAX_SCAN_BYTES + 1)
            if len(data) > MAX_SCAN_BYTES:
                if path.suffix == ".py":
                    raise ValueError(f"Python 탐지 크기 상한 초과: {relative.as_posix()}")
                continue
            if b"\0" in data:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            yield relative.as_posix(), text


def _text_sites(text: str) -> Iterator[tuple[int, str, Literal["warning"], None]]:
    # 비-Python은 의미를 확정할 AST가 없으므로 후보 위치만 warning으로 전달한다.
    for line, content in enumerate(text.splitlines(), 1):
        found = {pattern for pattern, regex in PATTERNS.items() if regex.search(content)}
        if any(_address_severity(m.group()) is not None for m in _TEXT_ADDRESS.finditer(content)):
            found.add("local_address")
        for pattern in sorted(found):
            yield line, pattern, "warning", None


def _setting_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _string(node.slice)
    return None


def _string(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _settings(tree: ast.AST) -> Iterator[tuple[str, ast.expr]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (name := _setting_name(target)) is not None:
                    yield name, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if (name := _setting_name(node.target)) is not None:
                yield name, node.value
        elif isinstance(node, ast.keyword) and node.arg is not None:
            yield node.arg, node.value
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (name := _string(key)) is not None:
                    yield name, value


def _address_severity(value: str) -> Literal["patch", "warning"] | None:
    # URL 전체나 단독 hostname만 판정한다. 설명문 부분 일치는 제외한다.
    host = value
    if "://" in value or value.startswith("//"):
        try:
            host = urlsplit(value).hostname or ""
        except ValueError:
            return None
    host = host.lower().rstrip(".").strip("[]")
    if host == "localhost":
        return "patch"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return "patch"
    return "warning" if address.is_private else None


def _numeric_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd | ast.USub):
        node = node.operand
    return isinstance(node, ast.Constant) and type(node.value) in (int, float)


def address_env_key(name: str, value: str) -> str | None:
    """앱의 DB 설정 이름과 런타임 주입 이름을 구분한다. 값은 반환하지 않는다."""
    scheme = value.partition("://")[0].partition("+")[0].lower() if "://" in value else ""
    if name in DATABASE_SETTINGS or scheme in {
        "mysql",
        "mariadb",
        "postgres",
        "postgresql",
        "sqlite",
    }:
        return "DATABASE_URL"
    return name if _ENV_NAME.fullmatch(name) else None


def environment_reads(
    tree: ast.AST, *, strict: bool = True
) -> Iterator[tuple[ast.AST, ast.AST | None]]:
    """별칭을 따라 읽기를 찾는다. 탐지는 별표 import를 허용하고 편집은 거부한다."""
    os_names = {"os"}
    environ_names: set[str] = set()
    getenv_names = {"getenv", "require_env", "env_bool", "env_int"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_names.update(a.asname or "os" for a in node.names if a.name == "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            if any(a.name == "*" for a in node.names):
                if strict:
                    raise ValueError("os 별표 import의 환경키 사용을 검증할 수 없다")
                environ_names.add("environ")
            environ_names.update(a.asname or a.name for a in node.names if a.name == "environ")
            getenv_names.update(a.asname or a.name for a in node.names if a.name == "getenv")

    def is_environ(node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id in environ_names) or (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id in os_names
        )

    def is_reader(node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id in getenv_names) or (
            isinstance(node, ast.Attribute)
            and (
                (
                    is_environ(node.value)
                    and node.attr in {"get", "setdefault", "pop", "__getitem__"}
                )
                or (
                    node.attr == "getenv"
                    and isinstance(node.value, ast.Name)
                    and node.value.id in os_names
                )
            )
        )

    # 모듈·environ·읽기 함수의 정적 별칭 체인도 빠뜨리지 않는다.
    while True:
        previous = (set(os_names), set(environ_names), set(getenv_names))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                value, targets = node.value, node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                value, targets = node.value, [node.target]
            else:
                continue
            bound = {t.id for t in targets if isinstance(t, ast.Name)}
            if isinstance(value, ast.Name) and value.id in os_names:
                os_names.update(bound)
            if is_environ(value):
                environ_names.update(bound)
            if is_reader(value):
                getenv_names.update(bound)
        if previous == (os_names, environ_names, getenv_names):
            break

    for node in ast.walk(tree):
        key: ast.AST | None = None
        read = False
        if isinstance(node, ast.Subscript) and is_environ(node.value):
            key, read = node.slice, True
        elif isinstance(node, ast.Call):
            read = is_reader(node.func)
            if read:
                key = (
                    node.args[0]
                    if node.args
                    else next((kw.value for kw in node.keywords if kw.arg == "key"), None)
                )
        if read:
            yield node, key


def _scan_file(tree: ast.AST) -> Iterator[tuple[int, str, Literal["patch", "warning"], str | None]]:
    env_nodes = {
        node for read, _ in environment_reads(tree, strict=False) for node in ast.walk(read)
    }
    names: dict[ast.AST, str] = {}
    for name, value in _settings(tree):
        for child in ast.walk(value):
            names[child] = name
        if value in env_nodes:
            continue
        literal = _string(value)
        if name == STORAGE_ENV_KEY and literal and "://" not in literal:
            yield value.lineno, "local_storage_dir", "patch", STORAGE_ENV_KEY
        if name.lower() == "secret_key" and _string(value) is not None:
            yield value.lineno, "secret_key", "patch", "SECRET_KEY"
        elif (
            name == "SESSION_COOKIE_SECURE"
            and isinstance(value, ast.Constant)
            and type(value.value) is bool
        ):
            yield value.lineno, "cookie_secure", "patch", "SESSION_COOKIE_SECURE"

    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and _string(node.body[0].value) is not None
    }
    proxy_names = {"ProxyFix"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "werkzeug.middleware.proxy_fix":
            proxy_names.update(a.asname or a.name for a in node.names if a.name == "ProxyFix")
    for node in ast.walk(tree):
        if node in env_nodes:
            continue
        if (
            isinstance(node, ast.Constant)
            and node not in docstrings
            and (value := _string(node)) is not None
            and (severity := _address_severity(value)) is not None
        ):
            name = names.get(node, "")
            yield (
                node.lineno,
                "local_address",
                severity,
                address_env_key(name, value),
            )
        if isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id in proxy_names)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "ProxyFix")
        ):
            for kw in node.keywords:
                if kw.arg in {"x_for", "x_proto"} and _numeric_literal(kw.value):
                    yield kw.value.lineno, "proxy_fix", "patch", f"PROXY_FIX_{kw.arg.upper()}"


def scan_patch_targets(
    root: Path, changed_paths: Collection[str], *, bootstrap: bool = False
) -> tuple[PatchTarget, ...]:
    """전체 build 트리에서 Python AST 패치 대상과 비-Python 텍스트 경고 위치를 찾는다."""
    changed = frozenset(changed_paths)
    targets: dict[tuple[str, int, str, str | None], PatchTarget] = {}
    for relative, text in iter_source_texts(root):
        path = Path(relative)
        if path.suffix == ".py":
            try:
                tree = ast.parse(text, filename=relative)
            except (SyntaxError, ValueError):
                continue
            sites = _scan_file(tree)
        else:
            sites = _text_sites(text)
        warning_only = bool(_WARNING_PARTS.intersection(path.parts)) or path.name.startswith(
            "test_"
        )
        warning_only = warning_only or path.name.endswith("_test.py") or path.name == "conftest.py"
        for line, pattern, severity, key in sites:
            if pattern not in PATTERNS:
                continue
            target = PatchTarget(
                file=relative,
                line=line,
                pattern_id=pattern,
                is_new=bootstrap or relative in changed,
                severity="warning" if warning_only else severity,
                key=key,
            )
            targets[(relative, line, pattern, key)] = target
    return tuple(
        sorted(targets.values(), key=lambda t: (t.file, t.line, t.pattern_id, t.key or ""))
    )
