"""AI 설정 패치 검사기: 정상 패치 통과, 범위·비밀값·문법·적용 실패를 거부한다."""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from ddak.core.snapshots import file_manifest
from ddak.plan.patch.check import MAX_PATCH_BYTES, PatchPolicy, check_patch

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
