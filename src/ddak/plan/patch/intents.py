"""허용된 위치 의도를 필수 환경변수 읽기로 렌더한다. AI·실행·원본 수정은 없다."""

from __future__ import annotations

import ast
import ipaddress
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ddak.core.contracts.plan_facts import EnvKey, PatchTarget
from ddak.core.patch_patterns import PATTERNS
from ddak.core.snapshots import file_manifest
from ddak.plan.patch.check import build_patch

__all__ = ["EditIntent", "render_intents"]

_ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SECRET = re.compile(r"SECRET|PASSWORD|PASSWD|TOKEN|PRIVATE|CREDENTIAL|API_KEY|_KEY$")
_DATABASE = frozenset({"DATABASE", "DATABASE_URL", "SQLALCHEMY_DATABASE_URI", "DB_URL", "DB_URI"})


def _safe_file(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or path.suffix != ".py"
        or any(part.startswith(".") for part in path.parts)
        or {"test", "tests", "migration", "migrations"}.intersection(path.parts)
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
        or path.name == "conftest.py"
        or any(char in value for char in "\\\"'\t\r\n\0")
    ):
        raise ValueError("허용된 정규 Python 소스 경로가 필요하다")
    return value


class EditIntent(BaseModel):
    """AI가 지정할 수 있는 것은 위치와 환경키 이름뿐이다."""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )

    file: str
    line: int = Field(ge=1)
    pattern_id: str
    key: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")

    _file_path = field_validator("file")(_safe_file)

    @field_validator("pattern_id")
    @classmethod
    def known_pattern(cls, value: str) -> str:
        if value not in PATTERNS:
            raise ValueError("허용되지 않는 패턴이다")
        return value


@dataclass(frozen=True)
class _Site:
    node: ast.AST
    pattern: str
    key: str | None
    conversion: str = "str"


def _string(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and type(node.value) is str else None


def _setting_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return _string(node.slice) if isinstance(node, ast.Subscript) else None


def _settings(tree: ast.AST) -> Iterator[tuple[str, ast.AST]]:
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


def _loopback(value: str) -> bool:
    host = value
    if "://" in value or value.startswith("//"):
        try:
            host = urlsplit(value).hostname or ""
        except ValueError:
            return False
    host = host.lower().rstrip(".").strip("[]")
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _dev_database(name: str, value: str) -> bool:
    return name in _DATABASE and (
        value.startswith(("sqlite:///", "sqlite+pysqlite:///"))
        or value == ":memory:"
        or ("://" not in value and value.endswith((".sqlite", ".sqlite3", ".db")))
    )


def _sites(tree: ast.Module) -> list[_Site]:
    sites: list[_Site] = []
    names: dict[ast.AST, str] = {}
    development: set[ast.AST] = set()
    for name, value in _settings(tree):
        for child in ast.walk(value):
            names[child] = name
        if (text := _string(value)) is not None and _dev_database(name, text):
            development.add(value)
        if name.lower() == "secret_key" and _string(value) is not None:
            sites.append(_Site(value, "secret_key", "SECRET_KEY"))
        elif (
            name == "SESSION_COOKIE_SECURE"
            and isinstance(value, ast.Constant)
            and type(value.value) is bool
        ):
            sites.append(_Site(value, "cookie_secure", name, "bool"))

    excluded: set[ast.AST] = set()
    for key in _environment_reads(tree):
        if key is not None:
            excluded.update(ast.walk(key))
    proxy_names = {"ProxyFix"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and _string(node.body[0].value) is not None
        ):
            excluded.add(node.body[0].value)
        if isinstance(node, ast.JoinedStr):
            excluded.update(ast.walk(node))
        if isinstance(node, ast.Subscript):
            excluded.update(ast.walk(node.slice))
        if isinstance(node, ast.ImportFrom) and node.module == "werkzeug.middleware.proxy_fix":
            proxy_names.update(a.asname or a.name for a in node.names if a.name == "ProxyFix")

    for node in ast.walk(tree):
        if node not in excluded and (value := _string(node)) is not None:
            name = names.get(node, "")
            if _loopback(value) or node in development:
                sites.append(
                    _Site(node, "local_address", name if _ENV_KEY.fullmatch(name) else None)
                )
        if isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id in proxy_names)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "ProxyFix")
        ):
            for kw in node.keywords:
                value = kw.value
                literal = value.operand if isinstance(value, ast.UnaryOp) else value
                if (
                    kw.arg in {"x_for", "x_proto"}
                    and isinstance(literal, ast.Constant)
                    and type(literal.value) in {int, float}
                    and (
                        not isinstance(value, ast.UnaryOp)
                        or isinstance(value.op, ast.UAdd | ast.USub)
                    )
                ):
                    sites.append(_Site(value, "proxy_fix", f"PROXY_FIX_{kw.arg.upper()}", "int"))
    # 다중 대입은 동일 노드를 반복할 수 있으므로 실제 리터럴 기준으로 합친다.
    return list({(s.node, s.pattern, s.key): s for s in sites}.values())


def _bound_names(tree: ast.AST) -> Iterator[tuple[str, ast.AST]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            yield node.id, node
        elif isinstance(node, ast.arg):
            yield node.arg, node
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield node.name, node
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                yield alias.asname or alias.name.split(".")[0], node
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and node.name:
            yield node.name, node
        elif isinstance(node, ast.MatchMapping) and node.rest:
            yield node.rest, node


def _check_bindings(tree: ast.Module, *, needs_int: bool) -> bool:
    imports = []
    for name, node in _bound_names(tree):
        if name == "*":
            raise ValueError("별표 import의 이름 바인딩을 검증할 수 없다")
        if name == "os":
            if not (
                isinstance(node, ast.Import)
                and node in tree.body
                and all(
                    a.name == "os" or (a.name.startswith("os.") and a.asname is None)
                    for a in node.names
                    if (a.asname or a.name.split(".")[0]) == "os"
                )
            ):
                raise ValueError("os 이름이 다른 값으로 가려져 있다")
            imports.append(node)
        if needs_int and name == "int":
            raise ValueError("int 이름이 다른 값으로 가려져 있다")
    return bool(imports)


def _environment_reads(tree: ast.Module) -> Iterator[ast.AST | None]:
    """정적 import/대입 별칭을 따라 환경키 인자 AST를 찾는다."""
    os_names = {"os"}
    environ_names: set[str] = set()
    getenv_names = {"getenv", "require_env", "env_bool", "env_int"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_names.update(a.asname or "os" for a in node.names if a.name == "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            if any(a.name == "*" for a in node.names):
                raise ValueError("os 별표 import의 환경키 사용을 검증할 수 없다")
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
            yield key


def _env_keys(tree: ast.Module) -> set[str]:
    """동적 키는 기존 키와의 충돌을 증명할 수 없어 거부한다."""
    keys: set[str] = set()
    for key in _environment_reads(tree):
        value = _string(key)
        if value is None:
            raise ValueError("동적 환경키가 있어 키 충돌을 검증할 수 없다")
        keys.add(value)
    return keys


def _offsets(data: bytes) -> list[int]:
    return [0, *(match.end() for match in re.finditer(b"\n", data))]


def _span(node: ast.AST, offsets: list[int]) -> tuple[int, int]:
    if node.end_lineno is None or node.end_col_offset is None:
        raise ValueError("AST 리터럴 범위가 없다")
    return (
        offsets[node.lineno - 1] + node.col_offset,
        offsets[node.end_lineno - 1] + node.end_col_offset,
    )


def _import_edit(tree: ast.Module, data: bytes, offsets: list[int]) -> tuple[int, int, bytes]:
    body = list(tree.body)
    prefix = None
    if body and isinstance(body[0], ast.Expr) and _string(body[0].value) is not None:
        prefix = body.pop(0)
    while body and isinstance(body[0], ast.ImportFrom) and body[0].module == "__future__":
        prefix = body.pop(0)
    if prefix is not None:
        position = offsets[prefix.end_lineno] if prefix.end_lineno < len(offsets) else len(data)
    else:
        position = 0
        for line in data.splitlines(keepends=True):
            if line.strip() and not line.lstrip().startswith(b"#"):
                break
            position += len(line)
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    leading = newline if position and not data[:position].endswith(b"\n") else b""
    return position, position, leading + b"import os" + newline


def render_intents(
    source: Path, targets: Sequence[PatchTarget], intents: Sequence[EditIntent]
) -> tuple[bytes, tuple[EnvKey, ...]]:
    """모든 patch 대상과 일대일로 대응한 의도만 원본 리터럴 범위에 적용한다.

    warning 대상은 읽기 전용이다. 실패는 소스값을 싣지 않는 ValueError다.
    dict 대상도 PatchTarget 계약으로 검증한다. 반환 diff는 check_patch 승인을 대신하지 않는다.
    """
    allowed = [PatchTarget.model_validate(t) if isinstance(t, Mapping) else t for t in targets]
    edits = [EditIntent.model_validate(i) for i in intents]
    if any(not isinstance(t, PatchTarget) for t in allowed):
        raise ValueError("PatchTarget 계약이 필요하다")
    identities = [(t.file, t.line, t.pattern_id, t.key) for t in allowed]
    if len(set(identities)) != len(identities):
        raise ValueError("대상 위치가 중복됐다")
    used: set[int] = set()
    planned: dict[str, list[tuple[PatchTarget, EditIntent]]] = {}
    keys: set[str] = set()
    for intent in edits:
        matches = [
            index
            for index, t in enumerate(allowed)
            if (t.file, t.line, t.pattern_id) == (intent.file, intent.line, intent.pattern_id)
            and t.key in {None, intent.key}
        ]
        if len(matches) != 1:
            raise ValueError("의도가 정확히 하나의 허용 위치와 대응하지 않는다")
        index = matches[0]
        target = allowed[index]
        if target.severity != "patch":
            raise ValueError("warning 대상은 변경할 수 없다")
        if index in used or intent.key in keys:
            raise ValueError("중복 의도 또는 환경키 충돌이다")
        used.add(index)
        keys.add(intent.key)
        planned.setdefault(intent.file, []).append((target, intent))
    if used != {i for i, t in enumerate(allowed) if t.severity == "patch"}:
        raise ValueError("모든 patch 대상에 정확히 하나의 의도가 필요하다")
    if not edits:
        return b"", ()

    manifest = file_manifest(source)
    originals: dict[str, bytes] = {}
    trees: dict[str, ast.Module] = {}
    existing: set[str] = set()
    for file in sorted(manifest):
        if not file.endswith(".py"):
            continue
        data = (source / file).read_bytes()
        try:
            tree = ast.parse(data.decode("utf-8"), filename=file)
        except (UnicodeDecodeError, SyntaxError, ValueError):
            raise ValueError("환경키 검사를 위한 Python 소스를 파싱할 수 없다") from None
        existing.update(_env_keys(tree))
        if file in planned:
            originals[file], trees[file] = data, tree
    if keys & existing:
        raise ValueError("이미 사용 중인 환경키를 가져올 수 없다")
    if set(planned) - set(originals):
        raise ValueError("대상이 원본 manifest의 일반 Python 파일이 아니다")

    changes: dict[str, tuple[str, str]] = {}
    env: list[EnvKey] = []
    for file, pairs in sorted(planned.items()):
        data, tree = originals[file], trees[file]
        offsets = _offsets(data)
        candidates = _sites(tree)
        replacements: dict[tuple[int, int], bytes] = {}
        needs_int = False
        for target, intent in sorted(pairs, key=lambda pair: (pair[1].line, pair[1].key)):
            matches = [
                site
                for site in candidates
                if (site.node.lineno, site.pattern) == (intent.line, intent.pattern_id)
                and site.key in {None, intent.key}
                and (target.key is None or site.key == target.key)
            ]
            if len(matches) != 1:
                raise ValueError("위치가 하나의 지원 리터럴과 대응하지 않는다")
            site = matches[0]
            span = _span(site.node, offsets)
            if span in replacements:
                raise ValueError("같은 리터럴을 두 번 편집할 수 없다")
            value = f"os.environ['{intent.key}']"
            if site.conversion == "bool":
                value = f"({value}.lower() == 'true')"
            elif site.conversion == "int":
                value = f"int({value})"
                needs_int = True
            replacements[span] = value.encode("ascii")
            env.append(
                EnvKey(name=intent.key, kind="secret" if _SECRET.search(intent.key) else "plain")
            )
        if not _check_bindings(tree, needs_int=needs_int):
            start, end, replacement = _import_edit(tree, data, offsets)
            if any(begin < start for begin, _ in replacements):
                raise ValueError("리터럴보다 뒤에 os import를 안전하게 삽입할 수 없다")
            replacements[(start, end)] = replacement
        else:
            first_import = min(
                node.lineno
                for node in tree.body
                if isinstance(node, ast.Import)
                and any(
                    (a.name == "os" and a.asname in {None, "os"})
                    or (a.name.startswith("os.") and a.asname is None)
                    for a in node.names
                )
            )
            if any(intent.line <= first_import for _, intent in pairs):
                raise ValueError("환경변수 읽기 전에 module os import가 필요하다")
        ordered = sorted(replacements.items())
        if any(left[0][1] > right[0][0] for left, right in pairwise(ordered)):
            raise ValueError("편집 범위가 겹친다")
        rendered = data
        for (start, end), value in reversed(ordered):
            rendered = rendered[:start] + value + rendered[end:]
        try:
            ast.parse(rendered.decode("utf-8"), filename=file)
        except (SyntaxError, ValueError):
            raise ValueError("렌더 결과가 유효한 Python 소스가 아니다") from None
        changes[file] = (data.decode("utf-8"), rendered.decode("utf-8"))
    return build_patch(changes), tuple(sorted(env, key=lambda item: item.name))
