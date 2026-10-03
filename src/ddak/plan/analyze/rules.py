"""환경 키 추출·이름 기반 분류(규칙). 값은 읽지도 담지도 않는다. AI를 import하지 않는다."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Literal

from ddak.core.contracts.deploy_config import DeployConfig
from ddak.core.patch_patterns import MAX_SCAN_BYTES, iter_source_texts
from ddak.core.smoke import V2_BOX_MARK
from ddak.core.storage import STORAGE_ENV_KEY

_NAME = r"[A-Z][A-Z0-9_]{0,63}"
_EXAMPLE_LINE = re.compile(rf"^\s*(?:export\s+)?({_NAME})\s*=")  # 값 쪽은 캡처하지 않는다
_SECRET = re.compile(r"SECRET|PASSWORD|PASSWD|TOKEN|PRIVATE|CREDENTIAL|API_KEY|_KEY$")
_PLAIN = re.compile(
    r"^(?:DEBUG|PORT|HOST|LOG_LEVEL|ENV|TZ|WORKERS)$|_(?:ENABLED|SECURE|PORT|HOST|LEVEL)$"
)

Verdict = Literal["secret", "plain"] | None


def classify(name: str) -> Verdict:
    """secret 패턴이 plain 패턴보다 먼저다. None이면 애매(Jev에게 묻는다)."""
    if name in {"RELEASE_ID", "SOURCE_SHA", STORAGE_ENV_KEY}:
        return "plain"
    if _SECRET.search(name):
        return "secret"
    return "plain" if _PLAIN.search(name) else None


def example_keys(path: Path) -> list[str]:
    """env_example의 `NAME=` 줄에서 이름만 뽑는다."""
    if path.is_symlink() or (path.name.startswith(".env") and path.name != ".env.example"):
        return []
    try:
        if path.stat().st_size > MAX_SCAN_BYTES:
            return []
        with path.open("rb") as stream:
            data = stream.read(MAX_SCAN_BYTES + 1)
        if len(data) > MAX_SCAN_BYTES or b"\0" in data:
            return []
        text = data.decode("utf-8", errors="replace")
    except OSError:
        return []
    return [m.group(1) for line in text.splitlines() if (m := _EXAMPLE_LINE.match(line))]


def _is_environ(node: ast.AST, env_names: set[str], os_names: set[str]) -> bool:
    return (isinstance(node, ast.Name) and node.id in env_names) or (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id in os_names
    )


def _env_read(
    node: ast.AST, env_names: set[str], os_names: set[str], getenv_names: set[str]
) -> tuple[ast.AST, bool] | None:
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and _is_environ(node.value, env_names, os_names)
    ):
        return node.slice, True
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        if (isinstance(func, ast.Name) and func.id in getenv_names) or (
            isinstance(func, ast.Attribute)
            and (
                (func.attr == "get" and _is_environ(func.value, env_names, os_names))
                or (
                    func.attr == "getenv"
                    and isinstance(func.value, ast.Name)
                    and func.value.id in os_names
                )
            )
        ):
            return node.args[0], False
    return None


def source_key_reads(root: Path, rel: str) -> list[tuple[str, str, str, bool]]:
    """키·경로·값 없는 사용 형태·필수 여부. []는 필수, get/getenv는 선택이다."""
    out: list[tuple[str, str, str, bool]] = []
    relative = Path(rel)
    if relative.is_absolute() or ".." in relative.parts:
        return out
    for name, text in iter_source_texts(root, python_only=True):
        path = Path(name)
        if not (path == relative or path.is_relative_to(relative)):
            continue
        try:
            tree = ast.parse(text, filename=name)
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        os_names = {"os"}
        env_names: set[str] = set()
        getenv_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                os_names.update(a.asname or a.name for a in node.names if a.name == "os")
            elif isinstance(node, ast.ImportFrom) and node.module == "os" and node.level == 0:
                env_names.update(a.asname or a.name for a in node.names if a.name == "environ")
                getenv_names.update(a.asname or a.name for a in node.names if a.name == "getenv")

        for node in ast.walk(tree):
            read = _env_read(node, env_names, os_names, getenv_names)
            if read is None:
                continue
            key, required = read
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and re.fullmatch(_NAME, key.value)
            ):
                out.append(
                    (
                        key.value,
                        name,
                        f'os.getenv("{key.value}")',
                        required,
                    )
                )
        out.extend(
            (key, name, f'os.getenv("{key}")', required)
            for key, required in _wrapper_key_reads(tree, env_names, os_names, getenv_names)
        )
    return out


def source_keys(root: Path, rel: str) -> list[tuple[str, str, str]]:
    """(이름, 소스 상대경로, 사용 형태) 목록. 사용 형태는 기본값 인자를 버린 `os.getenv("X")`."""
    return [(name, path, snippet) for name, path, snippet, _ in source_key_reads(root, rel)]


def smoke_groups(cfg: DeployConfig, root: Path) -> tuple[str, ...]:
    """요청의 배포 트리에서 v2 템플릿 표식을 찾는다. 변경 diff·과거 배포는 보지 않는다."""
    paths = tuple(Path(rel) for tc in cfg.tiers.values() for rel in tc.paths)
    for name, text in iter_source_texts(root):
        path = Path(name)
        if path.suffix not in {".html", ".jinja", ".jinja2"}:
            continue
        if any(path == rel or path.is_relative_to(rel) for rel in paths) and V2_BOX_MARK in text:
            return ("v2",)
    return ()


def _missing_value_test(node: ast.AST, name: str) -> bool:
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return any(_missing_value_test(value, name) for value in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = node.operand
        return isinstance(value, ast.Name) and value.id == name
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        if not isinstance(node.ops[0], (ast.Is, ast.Eq)):
            return False
        left, right = node.left, node.comparators[0]
        return any(
            isinstance(value, ast.Name)
            and value.id == name
            and isinstance(missing, ast.Constant)
            and missing.value is None
            for value, missing in ((left, right), (right, left))
        )
    return False


def _guarded_parameters(fn, parameters, env_names, os_names, getenv_names) -> set[str]:
    """env 읽기 직후 결측 검사로 raise하는 일반 required wrapper를 보존한다."""
    required = set()
    for assignment, guard in zip(fn.body, fn.body[1:], strict=False):
        if not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1:
            continue
        variable = assignment.targets[0]
        read = _env_read(assignment.value, env_names, os_names, getenv_names)
        if (
            not isinstance(variable, ast.Name)
            or read is None
            or not isinstance(read[0], ast.Name)
            or read[0].id not in parameters
            or not isinstance(guard, ast.If)
            or not _missing_value_test(guard.test, variable.id)
        ):
            continue
        call = assignment.value
        if isinstance(call, ast.Call):
            defaults = [*call.args[1:], *(keyword.value for keyword in call.keywords)]
            if any(
                not isinstance(value, ast.Constant) or value.value is not None for value in defaults
            ):
                continue
        # 조건부 반환·값 변경을 추론하지 않고, 결측 분기의 직접 raise만 인정한다.
        for statement in guard.body:
            if isinstance(statement, ast.Raise):
                required.add(read[0].id)
                break
            if not isinstance(statement, ast.Expr):
                break
    return required


def _wrapper_key_reads(
    tree: ast.Module, env_names: set[str], os_names: set[str], getenv_names: set[str]
) -> list[tuple[str, bool]]:
    """같은 파일의 wrapper literal 호출에 원래 읽기의 필수 여부를 보존한다."""
    wrappers: dict[str, tuple[list[str], dict[str, bool]]] = {}
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args]]
        parameters = set(args) | {a.arg for a in fn.args.kwonlyargs}
        reads: dict[str, bool] = {}
        pending = list(fn.body)
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            read = _env_read(node, env_names, os_names, getenv_names)
            if read is not None:
                key, required = read
                if isinstance(key, ast.Name) and key.id in parameters:
                    reads[key.id] = reads.get(key.id, False) or required
            pending.extend(ast.iter_child_nodes(node))
        for parameter in _guarded_parameters(fn, parameters, env_names, os_names, getenv_names):
            reads[parameter] = True
        if reads:
            wrappers[fn.name] = args, reads

    found: dict[str, bool] = {}
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            continue
        if call.func.id not in wrappers:
            continue
        positional, reads = wrappers[call.func.id]
        # *args 뒤의 위치는 정적으로 확정할 수 없으므로 키 이름으로 추측하지 않는다.
        literal_args = []
        for arg in call.args:
            if isinstance(arg, ast.Starred):
                break
            literal_args.append(arg)
        bound = dict(zip(positional, literal_args, strict=False))
        bound.update((kw.arg, kw.value) for kw in call.keywords if kw.arg is not None)
        for parameter, required in reads.items():
            value = bound.get(parameter)
            if (
                isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and re.fullmatch(_NAME, value.value)
            ):
                found[value.value] = found.get(value.value, False) or required
    return sorted(found.items())
