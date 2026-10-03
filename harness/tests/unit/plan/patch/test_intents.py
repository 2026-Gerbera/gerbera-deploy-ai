"""위치 의도 렌더러: 실제 patch 적용·검사와 거부 경계를 확인한다."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from ddak.core.contracts.enums import By
from ddak.core.contracts.plan_facts import PatchTarget
from ddak.core.patch_patterns import scan_patch_targets
from ddak.core.snapshots import apply_diff, copy_source, file_manifest
from ddak.plan.patch.check import check_patch
from ddak.plan.patch.intents import EditIntent, render_intents


def write(source: Path, text: str | bytes, file: str = "app.py") -> None:
    path = source / file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)


def target(
    line: int = 1,
    pattern: str = "local_address",
    key: str | None = "HOST",
    file: str = "app.py",
    severity: str = "patch",
) -> PatchTarget:
    return PatchTarget(
        file=file, line=line, pattern_id=pattern, key=key, severity=severity, is_new=True
    )


def intent(t: PatchTarget, key: str | None = None) -> EditIntent:
    return EditIntent(file=t.file, line=t.line, pattern_id=t.pattern_id, key=key or t.key or "HOST")


def apply(source: Path, patch: bytes, tmp_path: Path) -> bytes:
    root = tmp_path / "applied"
    copy_source(source, root)
    apply_diff(root, patch)
    return (root / "app.py").read_bytes()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    return root


def test_all_supported_loopback_patterns_pass_real_checker(source: Path, tmp_path: Path) -> None:
    old = (
        '"""원본 문서."""\n'
        "from __future__ import annotations\n"
        "from werkzeug.middleware.proxy_fix import ProxyFix\n"
        "def create_app(app):\n"
        "    app.config.from_mapping(\n"
        "        SECRET_KEY='dev',\n"
        "        SESSION_COOKIE_SECURE=False,\n"
        "        DATABASE_URL='mysql://dev:dev@localhost/development',\n"
        "    )\n"
        "    app.config['APP_BASE_URL'] = 'http://127.0.0.1:5000/a?q=b'  # 한국어\n"
        "    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)\n"
        "    return app\n"
    )
    write(source, old)
    before = file_manifest(source)
    targets = scan_patch_targets(source, ["app.py"])
    assert len(targets) == 6
    intents = [intent(t) for t in targets]
    patch, env = render_intents(source, targets, intents)
    assert render_intents(source, list(reversed(targets)), list(reversed(intents))) == (patch, env)
    checked = check_patch(source, patch)
    assert checked.passed, checked.violations
    assert file_manifest(source) == before
    new = apply(source, patch, tmp_path).decode("utf-8")
    expected = old.replace(
        "from __future__ import annotations\n", "from __future__ import annotations\nimport os\n"
    )
    for old_value, new_value in {
        "'dev'": "os.environ['SECRET_KEY']",
        "False": "(os.environ['SESSION_COOKIE_SECURE'].lower() == 'true')",
        "'mysql://dev:dev@localhost/development'": "os.environ['DATABASE_URL']",
        "'http://127.0.0.1:5000/a?q=b'": "os.environ['APP_BASE_URL']",
        "x_for=1": "x_for=int(os.environ['PROXY_FIX_X_FOR'])",
        "x_proto=1": "x_proto=int(os.environ['PROXY_FIX_X_PROTO'])",
    }.items():
        expected = expected.replace(old_value, new_value)
    assert new == expected
    assert {e.name for e in env} == {i.key for i in intents}
    assert all(e.required and e.is_new and e.by is By.RULE for e in env)
    assert next(e for e in env if e.name == "SECRET_KEY").kind == "secret"


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("final_newline", [True, False])
def test_exact_newline_and_unicode_preservation(
    source: Path, tmp_path: Path, newline: bytes, final_newline: bool
) -> None:
    lines = [b"# coding: utf-8", "설명 = '한글'; HOST = 'localhost'  # 유지".encode()]
    # ';'를 허용하지 않는 검사기와 별개로 AST byte offset 자체를 확인한다.
    old = newline.join(lines) + (newline if final_newline else b"")
    write(source, old)
    t = target(line=2)
    patch, _ = render_intents(source, [t], [intent(t)])
    new = apply(source, patch, tmp_path)
    expected = old.replace(newline, newline + b"import os" + newline, 1)
    expected = expected.replace(b"'localhost'", b"os.environ['HOST']")
    assert new == expected
    assert (source / "app.py").read_bytes() == old


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("final_newline", [True, False])
def test_checker_passes_crlf_and_missing_final_newline(
    source: Path, tmp_path: Path, newline: bytes, final_newline: bool
) -> None:
    old = b"# original" + newline + b"HOST = 'localhost'" + (newline if final_newline else b"")
    write(source, old)
    t = target(line=2)
    patch, _ = render_intents(source, [t], [intent(t)])
    checked = check_patch(source, patch)
    assert checked.passed, checked.violations
    assert apply(source, patch, tmp_path) == (
        b"# original"
        + newline
        + b"import os"
        + newline
        + b"HOST = os.environ['HOST']"
        + (newline if final_newline else b"")
    )


@pytest.mark.parametrize(
    "header",
    [
        "#!/usr/bin/env python3\n# coding: utf-8\n",
        '"""여러 줄\n모듈 문서\n"""\n',
        '"""문서"""\n# future 사이 주석\nfrom __future__ import annotations\n',
        '"""문서"""\nfrom __future__ import annotations\nfrom __future__ import division\n',
        "@decorate\ndef function():\n    pass\n",
    ],
)
def test_import_is_inserted_without_changing_header(
    source: Path, tmp_path: Path, header: str
) -> None:
    old = header + "HOST = 'localhost'\n"
    write(source, old)
    t = target(line=old.count("\n"))
    patch, _ = render_intents(source, [t], [intent(t)])
    new = apply(source, patch, tmp_path).decode()
    assert new.replace("import os\n", "", 1) == old.replace("'localhost'", "os.environ['HOST']")
    tree = ast.parse(new)
    if header.startswith('"""'):
        assert ast.get_docstring(tree)
    if "__future__" in header:
        assert new.index("import os") > new.rindex("from __future__")
    if header.startswith("#!"):
        assert new.startswith("#!/usr/bin/env python3\n# coding: utf-8\n")
    checked = check_patch(source, patch)
    assert checked.passed, checked.violations


@pytest.mark.parametrize(
    "os_import", ["import os", "import os as os", "import sys, os", "import os.path"]
)
def test_existing_os_import_is_reused(source: Path, tmp_path: Path, os_import: str) -> None:
    write(source, os_import + "\nHOST = 'localhost'\n")
    t = target(line=2)
    patch, _ = render_intents(source, [t], [intent(t)])
    assert apply(source, patch, tmp_path) == (os_import + "\nHOST = os.environ['HOST']\n").encode()


@pytest.mark.parametrize(
    "binding",
    [
        "os = object()",
        "import sys as os",
        "import os, sys as os",
        "import os.path as os",
        "from package import os",
        "def f(os): pass",
        "def os(): pass",
        "class os: pass",
        "for os in []: pass",
        "[os for os in []]",
        "try: pass\nexcept Exception as os: pass",
        "match {}:\n    case {'x': os}: pass",
        "match {}:\n    case {**os}: pass",
        "del os",
        "from package import *",
        "from os import *",
        "def f():\n    HOST = 'localhost'\n    import os",
    ],
)
def test_shadowed_os_is_rejected(source: Path, binding: str) -> None:
    write(source, "HOST = 'localhost'\n" + binding + "\n")
    t = target()
    with pytest.raises(ValueError):
        render_intents(source, [t], [intent(t)])


@pytest.mark.parametrize(
    "read",
    [
        "import os\nOTHER = os.environ['HOST']",
        "import os\nOTHER = os.environ.get('HOST')",
        "import os\nOTHER = os.getenv('HOST')",
        "import os as system\nOTHER = system.environ['HOST']",
        "from os import environ as env\nOTHER = env.get('HOST')",
        "from os import getenv as read\nOTHER = read('HOST')",
        "import os\nenv = os.environ\nsecond = env\nOTHER = second['HOST']",
        "import os\nOTHER = os.environ.setdefault('HOST', 'value')",
        "import os\nread = os.environ.get\nOTHER = read('HOST')",
        "import os\nread = os.getenv\nOTHER = read('HOST')",
        "import os\nmodule = os\nOTHER = module.environ['HOST']",
        "OTHER = require_env('HOST')",
    ],
)
def test_existing_envkey_in_other_file_is_rejected(source: Path, read: str) -> None:
    write(source, "HOST = 'localhost'\n")
    write(source, read + "\n", "other.py")
    t = target()
    with pytest.raises(ValueError, match="사용 중"):
        render_intents(source, [t], [intent(t)])


def test_dynamic_envkey_fails_closed(source: Path) -> None:
    write(source, "HOST = 'localhost'\n")
    write(source, "import os\nOTHER = os.environ[name]\n", "other.py")
    t = target()
    with pytest.raises(ValueError, match="동적 환경키"):
        render_intents(source, [t], [intent(t)])


def test_wildcard_in_other_file_still_prevents_unverified_edit(source: Path) -> None:
    write(source, "HOST = 'localhost'\n")
    write(source, "from os import *\n", "other.py")
    t = target()
    with pytest.raises(ValueError, match="별표 import"):
        render_intents(source, [t], [intent(t)])


def test_intents_and_targets_are_one_to_one(source: Path) -> None:
    write(source, "HOST = 'localhost'\nSECOND_HOST = '127.0.0.1'\n")
    a, b = target(), target(line=2, key="SECOND_HOST")
    with pytest.raises(ValueError, match="모든 patch 대상"):
        render_intents(source, [a, b], [intent(a)])
    with pytest.raises(ValueError, match="중복"):
        render_intents(source, [a], [intent(a), intent(a)])
    with pytest.raises(ValueError, match="대상 위치가 중복"):
        render_intents(source, [a, a], [intent(a)])
    for wrong in [target(line=3), target(pattern="secret_key"), target(file="other.py")]:
        with pytest.raises(ValueError, match="허용 위치"):
            render_intents(source, [a], [intent(wrong)])
    with pytest.raises(ValueError):
        render_intents(source, [a], [intent(a, "STOLEN_KEY")])


def test_unknown_key_may_be_selected_but_not_collide(source: Path) -> None:
    write(source, "first('localhost')\nsecond('127.0.0.1')\n")
    a, b = target(key=None), target(line=2, key=None)
    with pytest.raises(ValueError, match="환경키 충돌"):
        render_intents(source, [a, b], [intent(a), intent(b)])
    patch, env = render_intents(source, [a, b], [intent(a, "FIRST_HOST"), intent(b, "SECOND_HOST")])
    checked = check_patch(source, patch)
    assert checked.passed, checked.violations
    assert [e.name for e in env] == ["FIRST_HOST", "SECOND_HOST"]


@pytest.mark.parametrize("value", ["10.0.0.1", "172.16.1.2", "192.168.1.2", "http://10.0.0.1/a"])
def test_private_addresses_are_never_edited(source: Path, value: str) -> None:
    write(source, f"HOST = '{value}'\n")
    warning = target(severity="warning")
    assert render_intents(source, [warning], []) == (b"", ())
    with pytest.raises(ValueError, match="warning"):
        render_intents(source, [warning], [intent(warning)])
    with pytest.raises(ValueError, match="지원 리터럴"):
        render_intents(source, [target()], [intent(target())])


@pytest.mark.parametrize("value", ["localhost", "http://localhost:5000/a", "127.2.3.4", "::1"])
def test_supported_hostname_and_url_literals(source: Path, tmp_path: Path, value: str) -> None:
    write(source, f"HOST = '{value}'\n")
    t = target()
    patch, _ = render_intents(source, [t], [intent(t)])
    assert apply(source, patch, tmp_path) == b"import os\nHOST = os.environ['HOST']\n"
    checked = check_patch(source, patch)
    assert checked.passed, checked.violations


@pytest.mark.parametrize(
    "value", ["sqlite:///dev.db", "sqlite+pysqlite:///:memory:", "dev.sqlite3"]
)
def test_development_database_literal(source: Path, tmp_path: Path, value: str) -> None:
    write(source, f"DATABASE_URL = '{value}'\n")
    t = target(key="DATABASE_URL")
    patch, _ = render_intents(source, [t], [intent(t)])
    assert (
        apply(source, patch, tmp_path) == b"import os\nDATABASE_URL = os.environ['DATABASE_URL']\n"
    )


@pytest.mark.parametrize(
    ("text", "pattern", "key"),
    [
        ("SECRET_KEY: str = 'dev'\n", "secret_key", "SECRET_KEY"),
        ("app.config['SECRET_KEY'] = 'dev'\n", "secret_key", "SECRET_KEY"),
        ("settings = {'SECRET_KEY': 'dev'}\n", "secret_key", "SECRET_KEY"),
        ("app.config.update(SECRET_KEY='dev')\n", "secret_key", "SECRET_KEY"),
        ("SESSION_COOKIE_SECURE = True\n", "cookie_secure", "SESSION_COOKIE_SECURE"),
    ],
)
def test_setting_shapes_replace_only_value(
    source: Path, tmp_path: Path, text: str, pattern: str, key: str
) -> None:
    write(source, text)
    t = target(pattern=pattern, key=key)
    patch, _ = render_intents(source, [t], [intent(t)])
    value = f"os.environ['{key}']"
    if pattern == "cookie_secure":
        value = f"({value}.lower() == 'true')"
    expected = "import os\n" + text.replace(
        "True" if pattern == "cookie_secure" else "'dev'", value
    )
    assert apply(source, patch, tmp_path).decode() == expected


@pytest.mark.parametrize("number", ["1", "0", "-1", "+2", "1.0"])
def test_proxyfix_numeric_literals(source: Path, tmp_path: Path, number: str) -> None:
    write(source, f"app = ProxyFix(app, x_for={number})\n")
    t = target(pattern="proxy_fix", key="PROXY_FIX_X_FOR")
    patch, _ = render_intents(source, [t], [intent(t)])
    assert apply(source, patch, tmp_path) == (
        b"import os\napp = ProxyFix(app, x_for=int(os.environ['PROXY_FIX_X_FOR']))\n"
    )
    checked = check_patch(source, patch)
    assert checked.passed, checked.violations


def test_ambiguous_same_line_literals_are_rejected(source: Path) -> None:
    write(source, "hosts = ['localhost', '127.0.0.1']\n")
    t = target(key=None)
    with pytest.raises(ValueError, match="하나의 지원 리터럴"):
        render_intents(source, [t], [intent(t)])


def test_overlapping_patterns_are_rejected(source: Path) -> None:
    write(source, "SECRET_KEY = 'localhost'\n")
    a = target(pattern="secret_key", key="SECRET_KEY")
    b = target(key="SECRET_KEY")
    with pytest.raises(ValueError):
        render_intents(source, [a, b], [intent(a), intent(b)])


@pytest.mark.parametrize(
    ("text", "pattern", "key"),
    [
        ("# HOST = 'localhost'\n", "local_address", "HOST"),
        ('"""localhost"""\n', "local_address", "HOST"),
        ("HOST = 'contains localhost text'\n", "local_address", "HOST"),
        ("HOST = f'http://localhost/{port}'\n", "local_address", "HOST"),
        ("SECRET_KEY = create_secret()\n", "secret_key", "SECRET_KEY"),
        ("SECRET_KEY = b'dev'\n", "secret_key", "SECRET_KEY"),
        ("SESSION_COOKIE_SECURE = 0\n", "cookie_secure", "SESSION_COOKIE_SECURE"),
        ("app = ProxyFix(app, x_for=get_hops())\n", "proxy_fix", "PROXY_FIX_X_FOR"),
        ("import os\nHOST = os.getenv('localhost')\n", "local_address", "HOST"),
    ],
)
def test_stale_or_unsupported_literal_targets_rejected(
    source: Path, text: str, pattern: str, key: str
) -> None:
    write(source, text)
    t = target(pattern=pattern, key=key)
    with pytest.raises(ValueError):
        render_intents(source, [t], [intent(t)])


@pytest.mark.parametrize(
    "change",
    [
        {"line": "1"},
        {"line": True},
        {"line": 0},
        {"key": 123},
        {"key": "HOST']; attack() #"},
        {"key": "HOST\n"},
        {"pattern_id": "arbitrary_code"},
        {"file": "../app.py"},
        {"file": "/app.py"},
        {"file": "./app.py"},
        {"file": "nested//app.py"},
        {"file": "tests/app.py"},
        {"file": ".env.py"},
        {"code": "attack()"},
        {"value": "secret"},
    ],
)
def test_editintent_is_strict_and_forbids_code_and_values(change: dict) -> None:
    fields = {"file": "app.py", "line": 1, "pattern_id": "local_address", "key": "HOST"}
    fields.update(change)
    with pytest.raises(ValidationError):
        EditIntent.model_validate(fields)


def test_dict_targets_are_accepted(source: Path) -> None:
    write(source, "HOST = 'localhost'\n")
    t = target()
    patch, _ = render_intents(source, [t.model_dump()], [intent(t)])
    checked = check_patch(source, patch)
    assert checked.passed, checked.violations


def test_missing_file_and_symlink_fail(source: Path, tmp_path: Path) -> None:
    t = target()
    with pytest.raises(ValueError):
        render_intents(source, [t], [intent(t)])
    write(tmp_path, "HOST = 'localhost'\n", "outside.py")
    (source / "app.py").symlink_to(tmp_path / "outside.py")
    with pytest.raises(ValueError):
        render_intents(source, [t], [intent(t)])


def test_proxyfix_rejects_shadowed_int(source: Path) -> None:
    write(source, "app = ProxyFix(app, x_for=1)\nint = other\n")
    t = target(pattern="proxy_fix", key="PROXY_FIX_X_FOR")
    with pytest.raises(ValueError, match="int"):
        render_intents(source, [t], [intent(t)])


def test_late_os_import_fails(source: Path) -> None:
    write(source, "HOST = 'localhost'\nimport os\n")
    t = target()
    with pytest.raises(ValueError, match="module os import"):
        render_intents(source, [t], [intent(t)])


def test_empty_input_is_noop(source: Path) -> None:
    assert render_intents(source, [], []) == (b"", ())


def test_unvalidated_model_copy_is_revalidated(source: Path) -> None:
    write(source, "HOST = 'localhost'\n")
    t = target()
    invalid = intent(t).model_copy(update={"key": "HOST']; bad() #"})
    with pytest.raises(ValidationError):
        render_intents(source, [t], [invalid])


def test_proxyfix_import_alias_is_supported(source: Path) -> None:
    write(
        source, "from werkzeug.middleware.proxy_fix import ProxyFix as PF\napp = PF(app, x_for=1)\n"
    )
    targets = scan_patch_targets(source, ["app.py"])
    patch, _ = render_intents(source, targets, [intent(t) for t in targets])
    checked = check_patch(source, patch)
    assert checked.passed, checked.violations


def test_nested_setting_key_matches_core(source: Path, tmp_path: Path) -> None:
    write(source, "SETTINGS = {'HOST': 'localhost'}\n")
    targets = scan_patch_targets(source, ["app.py"])
    assert targets[0].key == "HOST"
    patch, _ = render_intents(source, targets, [intent(t) for t in targets])
    assert apply(source, patch, tmp_path) == b"import os\nSETTINGS = {'HOST': os.environ['HOST']}\n"
    checked = check_patch(source, patch)
    assert checked.passed, checked.violations


def test_database_inside_expression_is_not_rewritten(source: Path) -> None:
    write(source, "DATABASE = join(instance_path, 'dev.sqlite')\n")
    t = target(key="DATABASE")
    with pytest.raises(ValueError, match="지원 리터럴"):
        render_intents(source, [t], [intent(t)])
