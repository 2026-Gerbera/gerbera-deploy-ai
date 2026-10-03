"""AI 설정 패치 검사기: 정상 패치 통과, 범위·비밀값·문법·적용 실패를 거부한다."""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from ddak.core.snapshots import file_manifest
from ddak.plan.patch.check import MAX_PATCH_BYTES, PatchPolicy, build_patch, check_patch

APP = "flaskr/__init__.py"
ORIGINAL = """from flask import Flask


def create_app():
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY="dev",
        SESSION_COOKIE_SECURE=False,
    )
    app.config["APP_BASE_URL"] = "http://localhost:5000"
    return app
"""
GOOD = """import os

from flask import Flask


def create_app():
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ["SECRET_KEY"],
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false") == "true",
    )
    app.config["APP_BASE_URL"] = os.environ.get("APP_BASE_URL", "http://localhost:5000")
    return app
"""


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    (root / "flaskr").mkdir(parents=True)
    (root / APP).write_text(ORIGINAL, encoding="utf-8")
    (root / "migrations").mkdir()
    (root / "migrations" / "env.py").write_text('URL = "localhost"\n', encoding="utf-8")
    return root


def diff(new: str, path: str = APP, old: str = ORIGINAL) -> bytes:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(lines).encode()


def codes(result) -> set[str]:  # type: ignore[no-untyped-def]
    return {v.code for v in result.violations}


def test_good_patch_passes_and_leaves_source_untouched(source: Path) -> None:
    before = file_manifest(source)
    result = check_patch(source, diff(GOOD))
    assert result.passed, result.violations
    assert result.files == [APP]
    assert set(result.patterns) >= {"secret_key", "cookie_secure", "local_address"}
    assert result.patch_sha256 and result.patch_sha256.startswith("sha256:")
    assert file_manifest(source) == before


def test_dev_secret_kept_as_default_is_rejected(source: Path) -> None:
    bad = GOOD.replace('os.environ["SECRET_KEY"]', 'os.environ.get("SECRET_KEY", "dev")')
    result = check_patch(source, diff(bad))
    assert not result.passed
    assert "secret_literal" in codes(result)
    assert all("dev" not in v.message for v in result.violations)  # 줄 내용을 싣지 않는다


def test_unrelated_change_is_rejected(source: Path) -> None:
    bad = GOOD.replace("app = Flask(__name__)", 'app = Flask("other")')
    result = check_patch(source, diff(bad))
    assert {"pattern"} <= codes(result)
    lines = {v.line for v in result.violations if v.code == "pattern"}
    assert lines  # 줄 번호로 알려 준다


def test_dangerous_call_hidden_in_env_line_is_rejected(source: Path) -> None:
    bad = GOOD.replace('os.environ["SECRET_KEY"]', 'os.environ["SECRET_KEY"] or os.system("id")')
    assert "dangerous" in codes(check_patch(source, diff(bad)))


def test_pattern_removed_without_env_read_is_rejected(source: Path) -> None:
    bad = ORIGINAL.replace('SECRET_KEY="dev"', "SECRET_KEY=SECRET_KEY_VALUE")
    assert "env_read" in codes(check_patch(source, diff(bad)))


@pytest.mark.parametrize(
    ("policy", "path"),
    [
        (PatchPolicy(), "migrations/env.py"),
        (PatchPolicy(allowed_files=frozenset({"flaskr/other.py"})), APP),
    ],
)
def test_file_outside_policy_is_rejected(source: Path, policy: PatchPolicy, path: str) -> None:
    old = (source / path).read_text(encoding="utf-8")
    new = old.replace('"localhost"', 'os.environ["DB_HOST"]').replace(
        '"http://localhost:5000"', 'os.environ["APP_BASE_URL"]'
    )
    assert "scope" in codes(check_patch(source, diff(new, path, old), policy))


def test_new_file_is_rejected(source: Path) -> None:
    patch = diff('import os\nX = os.environ["X"]\n', "flaskr/new.py", "").replace(
        b"--- a/flaskr/new.py", b"--- /dev/null"
    )
    assert "scope" in codes(check_patch(source, patch))


def test_syntax_error_after_apply_is_rejected(source: Path) -> None:
    bad = GOOD.replace('os.environ["SECRET_KEY"],', 'os.environ["SECRET_KEY"],,')
    result = check_patch(source, diff(bad))
    assert "syntax" in codes(result)


def test_patch_that_does_not_apply_is_rejected(source: Path) -> None:
    other_original = ORIGINAL.replace("SESSION_COOKIE_SECURE=False", "SESSION_COOKIE_SECURE=True")
    patch = diff(GOOD, old=other_original)  # 원본과 다른 문맥
    assert "apply" in codes(check_patch(source, patch))


@pytest.mark.parametrize(
    "patch",
    [b"", b"   \n", "--- a/x\n".encode("utf-16"), b"x" * (MAX_PATCH_BYTES + 1)],
)
def test_malformed_input_is_rejected(source: Path, patch: bytes) -> None:
    result = check_patch(source, patch)
    assert not result.passed and "format" in codes(result)


def test_patch_without_target_pattern_is_rejected(source: Path) -> None:
    only_comment = ORIGINAL.replace("def create_app():", "# 주석만 추가\ndef create_app():")
    result = check_patch(source, diff(only_comment))
    assert not result.passed and result.patterns == []
    assert "pattern" in codes(result)


# ---- 우회 시도(PR #7 리뷰 6번) ----
def test_git_header_for_other_file_is_rejected(source: Path) -> None:
    patch = b"diff --git a/Dockerfile b/Dockerfile\n" + diff(GOOD)
    result = check_patch(source, patch)
    assert not result.passed and "scope" in codes(result)


def test_header_only_change_to_other_file_is_rejected(source: Path) -> None:
    mode_change = b"diff --git a/Dockerfile b/Dockerfile\nold mode 100644\nnew mode 100755\n"
    result = check_patch(source, diff(GOOD) + mode_change)
    assert not result.passed and "scope" in codes(result)


@pytest.mark.parametrize("name", ["a/../flaskr/__init__.py", "a//etc/x.py", 'a/"q".py'])
def test_unsafe_path_is_rejected(source: Path, name: str) -> None:
    patch = diff(GOOD).replace(f"a/{APP}".encode(), name.encode(), 1)
    patch = patch.replace(f"b/{APP}".encode(), ("b/" + name[2:]).encode(), 1)
    assert "scope" in codes(check_patch(source, patch))


def test_truncated_hunk_is_rejected(source: Path) -> None:
    patch = diff(GOOD).rstrip(b"\n").rsplit(b"\n", 2)[0] + b"\n"
    assert "format" in codes(check_patch(source, patch))


def test_uppercase_secret_default_is_rejected(source: Path) -> None:
    bad = GOOD.replace('os.environ["SECRET_KEY"]', 'os.environ.get("SECRET_KEY", "DEVFALLBACK")')
    assert "secret_literal" in codes(check_patch(source, diff(bad)))


def test_pattern_word_in_comment_does_not_allow_code(source: Path) -> None:
    bad = GOOD.replace("    return app\n", "    app.debug = True  # SECRET_KEY\n    return app\n")
    assert "pattern" in codes(check_patch(source, diff(bad)))


def test_env_word_in_string_does_not_allow_code(source: Path) -> None:
    bad = GOOD.replace("    return app\n", '    app.name = "os.environ"\n    return app\n')
    assert "pattern" in codes(check_patch(source, diff(bad)))


def test_new_call_outside_allowlist_is_rejected_after_apply(source: Path) -> None:
    # 줄 검사의 위험 호출 목록에 없는 호출도 AST 비교가 막는다
    bad = GOOD.replace('os.environ["SECRET_KEY"]', 'os.environ["SECRET_KEY"] or os.kill(1, 9)')
    result = check_patch(source, diff(bad))
    assert not result.passed and "dangerous" in codes(result)


def test_concatenated_secret_name_is_rejected_after_apply(source: Path) -> None:
    # 비밀 이름·값을 이어붙여 줄 검사를 피해도 AST 비교가 막는다
    bad = GOOD.replace('        SECRET_KEY=os.environ["SECRET_KEY"],\n', "").replace(
        "    return app\n",
        '    app.config["SEC" + "RET_KEY"] = os.environ.get("APP_" + "KEY", "hard" + "coded")\n'
        "    return app\n",
    )
    result = check_patch(source, diff(bad))
    assert not result.passed and "secret_literal" in codes(result)
    assert all("hard" not in v.message for v in result.violations)


def test_new_import_outside_allowlist_is_rejected_after_apply(source: Path) -> None:
    bad = GOOD.replace("import os\n", "import os\nimport sys\n")
    assert "dangerous" in codes(check_patch(source, diff(bad)))


# ---- O1 apply_diff 형식(파일마다 ---/+++ 쌍 하나 + hunk 하나, Git 확장 헤더 없음) ----
def test_build_patch_makes_one_hunk_per_file_and_passes(source: Path) -> None:
    far = ORIGINAL + "\n" * 20 + "# end\n"  # 바뀐 곳이 멀리 떨어져도 hunk 하나
    (source / APP).write_text(far, encoding="utf-8")
    new = GOOD + "\n" * 20 + "# end\n"
    patch = build_patch({APP: (far, new), "flaskr/same.py": ("x = 1\n", "x = 1\n")})
    assert patch.count(b"@@ ") == 1 and b"same.py" not in patch
    assert check_patch(source, patch).passed


def test_build_patch_keeps_missing_final_newline(source: Path) -> None:
    old, new = ORIGINAL.rstrip("\n"), GOOD.rstrip("\n")
    (source / APP).write_text(old, encoding="utf-8")
    patch = build_patch({APP: (old, new)})
    assert b"\\ No newline at end of file" in patch
    assert check_patch(source, patch).passed


def test_build_patch_splits_only_on_newline(source: Path) -> None:
    # CRLF와 줄 안의 폼피드(\x0c)를 그대로 둔다(str.splitlines는 \x0c에서도 나눈다)
    old = ORIGINAL.replace("\n\n\ndef", "\n\n\x0c\ndef").replace("\n", "\r\n")
    new = GOOD.replace("\n\n\ndef", "\n\n\x0c\ndef").replace("\n", "\r\n")
    (source / APP).write_bytes(old.encode())
    patch = build_patch({APP: (old, new)})
    assert patch.count(b"@@ ") == 1 and b"\x0c\r\n" in patch
    assert check_patch(source, patch).passed


def test_git_extended_header_is_rejected_even_when_paths_match(source: Path) -> None:
    patch = f"diff --git a/{APP} b/{APP}\nindex 1111111..2222222 100644\n".encode() + diff(GOOD)
    assert "scope" in codes(check_patch(source, patch))


def test_second_hunk_under_one_header_is_rejected(source: Path) -> None:
    far = ORIGINAL + "\n" * 20 + 'URL = "localhost"\n'
    (source / APP).write_text(far, encoding="utf-8")
    new = GOOD + "\n" * 20 + 'URL = os.environ["URL"]\n'
    patch = diff(new, old=far)  # difflib 기본(문맥 3줄)은 떨어진 변경을 hunk 두 개로 만든다
    assert patch.count(b"@@ ") == 2
    assert "format" in codes(check_patch(source, patch))


def test_same_file_twice_is_rejected(source: Path) -> None:
    assert "format" in codes(check_patch(source, diff(GOOD) + diff(GOOD)))


# ---- 방어형 점검 지적 반영(검사기와 git apply의 해석 일치, 예외 대신 위반) ----
SMALL = 'import os\nHOST = "localhost"\nX = 1\n'


def _small(source: Path, text: str = SMALL, path: str = "flaskr/small.py") -> str:
    (source / path).write_text(text, encoding="utf-8")
    return path


def test_no_newline_marker_in_middle_of_hunk_is_rejected(source: Path) -> None:
    # git은 표시 바로 앞 줄의 줄바꿈을 지워 다음 문맥 줄을 붙인다. 검사기가 본 것과 달라진다
    path = _small(source)
    patch = (
        f"--- a/{path}\n+++ b/{path}\n@@ -2,2 +2,2 @@\n"
        '-HOST = "localhost"\n+HOST = os.environ["HOST"]  #\n'
        "\\ No newline at end of file\n X = 1\n"
    ).encode()
    result = check_patch(source, patch)
    assert not result.passed and "format" in codes(result)


def test_no_newline_marker_after_header_is_rejected(source: Path) -> None:
    path = _small(source)
    patch = f"--- a/{path}\n+++ b/{path}\n\\ No newline at end of file\n".encode()
    assert "format" in codes(check_patch(source, patch))


@pytest.mark.parametrize(
    ("old_header", "new_header"),
    [
        ("--- a/flaskr/new.py", "+++ b/flaskr/new.py"),  # old 줄 수 0인 없는 파일: git이 생성
        ("--- a/flaskr/new.py\t1970-01-01 00:00:00 +0000", "+++ b/flaskr/new.py"),
    ],
)
def test_creating_file_without_dev_null_is_rejected(
    source: Path, old_header: str, new_header: str
) -> None:
    patch = f'{old_header}\n{new_header}\n@@ -0,0 +1,2 @@\n+import os\n+K = os.environ["K"]\n'
    result = check_patch(source, patch.encode())  # 예외가 아니라 위반으로 돌려준다
    assert not result.passed and "scope" in codes(result)


def test_epoch_suffix_delete_is_rejected(source: Path) -> None:
    path = _small(source, "# note\n")
    patch = f"--- a/{path}\n+++ b/{path}\t1970-01-01 00:00:00 +0000\n@@ -1 +0,0 @@\n-# note\n"
    assert "scope" in codes(check_patch(source, patch.encode()))


@pytest.mark.parametrize("name", ["flaskr//small.py", "./flaskr/small.py", "flaskr/./small.py"])
def test_non_normalized_path_is_rejected(source: Path, name: str) -> None:
    _small(source)
    new = SMALL.replace('"localhost"', 'os.environ["HOST"]')
    patch = build_patch({"flaskr/small.py": (SMALL, new)}).replace(
        b"flaskr/small.py", name.encode()
    )
    assert "scope" in codes(check_patch(source, patch))


def test_huge_hunk_count_is_violation_not_exception(source: Path) -> None:
    path = _small(source)
    patch = f"--- a/{path}\n+++ b/{path}\n@@ -2,{'9' * 5000} +2,2 @@\n".encode()
    assert "format" in codes(check_patch(source, patch))


def test_unparsable_original_is_violation_not_exception(source: Path) -> None:
    # 원본 대상 줄에 문법 오류가 있으면 AST 비교를 할 수 없다. 통과시키지 않는다
    broken = 'import os\nSECRET_KEY = "x\n'
    path = _small(source, broken)
    fixed = 'import os\nSECRET_KEY = os.environ["SECRET_KEY"]\n'
    result = check_patch(source, build_patch({path: (broken, fixed)}))
    assert not result.passed and "syntax" in codes(result)


def test_non_utf8_original_is_violation_not_exception(source: Path) -> None:
    path = "flaskr/small.py"
    (source / path).write_bytes(b'import os\nHOST = "localhost"\n# caf\xe9\n')
    patch = (
        f"--- a/{path}\n+++ b/{path}\n@@ -1,2 +1,2 @@\n import os\n"
        '-HOST = "localhost"\n+HOST = os.environ["HOST"]\n'
    ).encode()
    result = check_patch(source, patch)  # 적용 단계(apply) 또는 읽기 단계(format)에서 위반
    assert not result.passed and codes(result) & {"apply", "format"}


# ---- 범위·패턴 규칙 중 테스트가 없던 것 ----
@pytest.mark.parametrize("path", ["Dockerfile", "tests/test_app.py"])
def test_disallowed_file_is_rejected(source: Path, path: str) -> None:
    (source / path).parent.mkdir(parents=True, exist_ok=True)
    (source / path).write_text('HOST = "localhost"\n', encoding="utf-8")
    patch = build_patch({path: ('HOST = "localhost"\n', 'HOST = os.environ["HOST"]\n')})
    assert "scope" in codes(check_patch(source, patch))


def test_delete_and_rename_are_rejected(source: Path) -> None:
    delete = diff(GOOD).replace(f"+++ b/{APP}".encode(), b"+++ /dev/null")
    rename = diff(GOOD).replace(f"+++ b/{APP}".encode(), b"+++ b/flaskr/other.py")
    assert "scope" in codes(check_patch(source, delete))
    assert "scope" in codes(check_patch(source, rename))


def test_too_many_files_is_rejected(source: Path) -> None:
    changes = {}
    for i in range(6):
        path = f"flaskr/m{i}.py"
        (source / path).write_text(SMALL, encoding="utf-8")
        changes[path] = (SMALL, SMALL.replace('"localhost"', 'os.environ["HOST"]'))
    assert "scope" in codes(check_patch(source, build_patch(changes)))


def test_semicolon_and_unknown_from_import_are_rejected(source: Path) -> None:
    semi = GOOD.replace("import os\n", "import os; X = 1\n")
    other = GOOD.replace("import os\n", "import os\nfrom somepkg import thing\n")
    assert "dangerous" in codes(check_patch(source, diff(semi)))
    assert "dangerous" in codes(check_patch(source, diff(other)))


def test_proxyfix_and_loopback_patterns_pass(source: Path) -> None:
    old = 'from flask import Flask\n\napp = Flask(__name__)\napp.config["DB_HOST"] = "127.0.0.1"\n'
    new = (
        "import os\n\nfrom flask import Flask\nfrom werkzeug.middleware.proxy_fix import ProxyFix\n"
        "\napp = Flask(__name__)\n"
        'app.config["DB_HOST"] = os.environ["DB_HOST"]\n'
        "app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)\n"
    )
    path = _small(source, old)
    result = check_patch(source, build_patch({path: (old, new)}))
    assert result.passed, result.violations
    assert {"local_address", "proxy_fix"} <= set(result.patterns)


@pytest.mark.parametrize(
    "line",
    [
        '    app.config.update({"SECRET_KEY": "x" + "y"})\n',
        '    app.config.update(SECRET_KEY="x" + "y")\n',
    ],
)
def test_secret_literal_in_dict_or_keyword_is_rejected(source: Path, line: str) -> None:
    bad = GOOD.replace("    return app\n", line + "    return app\n")
    assert "secret_literal" in codes(check_patch(source, diff(bad)))


def test_violation_messages_never_contain_line_contents(source: Path) -> None:
    marker = "MARKER_" + "LINE_CONTENT"
    bad = GOOD.replace(
        "    return app\n", f'    app.x = os.system("{marker}")  # {marker}\n    return app\n'
    )
    result = check_patch(source, diff(bad))
    assert result.violations
    assert all(marker not in v.message and marker not in v.file for v in result.violations)


SUBSCRIPT_ORIGINAL = """from flask import Flask


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "dev"
    return app
"""


def _subscript_check(source: Path, old_line: str, new_lines: str, imports: str):  # type: ignore[no-untyped-def]
    (source / APP).write_text(SUBSCRIPT_ORIGINAL, encoding="utf-8")
    new = SUBSCRIPT_ORIGINAL.replace(old_line, new_lines).replace(
        "from flask import Flask\n", imports + "from flask import Flask\n"
    )
    return check_patch(source, build_patch({APP: (SUBSCRIPT_ORIGINAL, new)}))


@pytest.mark.parametrize(
    ("value", "imports"),
    [
        ('os.environ["SECRET_KEY"]', "import os\n\n"),
        ('os.environ.get("SECRET_KEY")', "import os\n\n"),
        ('getenv("SECRET_KEY")', "from os import getenv\n\n"),
    ],
)
def test_config_subscript_secret_key_from_env_passes(
    source: Path, value: str, imports: str
) -> None:
    # 2차 데모 대표 패치. config 키 문자열 "SECRET_KEY"는 비밀값이 아니다
    result = _subscript_check(source, '"dev"', value, imports)
    assert result.passed, result.violations


DICT_ORIGINAL = """import os


def load():
    return {
        "APP_BASE_URL": "http://localhost:5000",
        "SECRET_KEY": "dev",
    }
"""


def _dict_check(source: Path, new_lines: str):  # type: ignore[no-untyped-def]
    path = "flaskr/config.py"
    (source / path).write_text(DICT_ORIGINAL, encoding="utf-8")
    new = DICT_ORIGINAL.replace('        "SECRET_KEY": "dev",', new_lines)
    return check_patch(source, build_patch({path: (DICT_ORIGINAL, new)}))


@pytest.mark.parametrize(
    "line",
    [
        '        "SECRET_KEY": os.environ["SECRET_KEY"],',
        '        "SECRET_KEY": os.environ.get("SECRET_KEY"),',
        '        **{"SECRET_KEY": os.getenv("SECRET_KEY")},',
    ],
)
def test_dict_key_secret_key_from_env_passes(source: Path, line: str) -> None:
    # 샘플 앱 config.load()가 딕셔너리라 실제 Claude 패치가 이 모양이다(10/3 실측에서 오탐)
    result = _dict_check(source, line)
    assert result.passed, result.violations


@pytest.mark.parametrize(
    "line",
    [
        '        "SECRET_KEY": "HUNTER" + "PW",',
        '        "SECRET_KEY": "HUNTERPW",',  # 대문자 값도 키 자리가 아니다
        '        "SECRET_KEY": os.environ.get("SECRET_KEY", "hunter" + "2pass"),',
    ],
)
def test_dict_value_secret_is_still_rejected(source: Path, line: str) -> None:
    result = _dict_check(source, line)
    assert "secret_literal" in codes(result)


def test_list_literal_secret_is_still_rejected(source: Path) -> None:
    result = _subscript_check(
        source, '"dev"', '["HUNTER" + "PW"][0] or os.environ["SECRET_KEY"]', "import os\n\n"
    )
    assert "secret_literal" in codes(result)


@pytest.mark.parametrize(
    ("line", "imports"),
    [
        # 별칭 import: getenv()가 실제로는 다른 함수다
        ('app.config["SECRET_KEY"] = getenv("SECRET_KEY")', "from os import getcwd as getenv\n"),
        ('app.config["SECRET_KEY"] = getenv("SECRET_KEY")', "from os import system as getenv\n"),
        ('app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]', "from flaskr import db as os\n"),
        # 대입으로 다시 묶기(줄 검사는 SECRET_KEY 패턴 때문에 통과한다)
        (
            'SECRET_KEY = getenv = os.remove\n    app.config["SECRET_KEY"] = getenv("SECRET_KEY")',
            "import os\n\n",
        ),
        (
            "SECRET_KEY = os.getenv = os.remove\n"
            '    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")',
            "import os\n\n",
        ),
        # * import는 어떤 이름이 묶이는지 알 수 없다
        ('app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]', "import os\nfrom os import *\n"),
    ],
)
def test_rebinding_allowed_call_names_is_rejected(source: Path, line: str, imports: str) -> None:
    result = _subscript_check(source, 'app.config["SECRET_KEY"] = "dev"', line, imports)
    assert not result.passed
    assert "dangerous" in codes(result)


@pytest.mark.parametrize(
    "line",
    [
        # 따옴표 없는 비밀값은 줄 검사·AST 모두 못 본다. 추가한 줄의 주석은 거부한다
        '    app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]  # was hunter2pass',
        "    # SECRET_KEY old value hunter2pass\n"
        '    app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]',
    ],
)
def test_comment_in_added_line_is_rejected(source: Path, line: str) -> None:
    result = _subscript_check(source, '    app.config["SECRET_KEY"] = "dev"', line, "import os\n\n")
    assert "comment" in codes(result)


@pytest.mark.parametrize(
    "lines",
    [
        # 패턴 단어(PROXY_FIX_) 이름에 값을 넣고 다음 줄에서 비밀 기본값으로 쓴다
        '    PROXY_FIX_DEFAULT = "hunter" + "2pass"\n'
        '    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", PROXY_FIX_DEFAULT)',
        '    PROXY_FIX_A = "hunter" + "2pass"\n'
        "    PROXY_FIX_B = PROXY_FIX_A\n"
        '    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", PROXY_FIX_B)',
    ],
)
def test_secret_value_through_other_name_is_rejected(source: Path, lines: str) -> None:
    result = _subscript_check(
        source, '    app.config["SECRET_KEY"] = "dev"', lines, "import os\n\n"
    )
    assert not result.passed
    assert "secret_literal" in codes(result)


def test_non_secret_env_default_still_passes(source: Path) -> None:
    lines = (
        '    app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]\n'
        '    app.config["APP_BASE_URL"] = os.environ.get("APP_BASE_URL", "http://localhost:5000")'
    )
    result = _subscript_check(
        source, '    app.config["SECRET_KEY"] = "dev"', lines, "import os\n\n"
    )
    assert result.passed, result.violations
