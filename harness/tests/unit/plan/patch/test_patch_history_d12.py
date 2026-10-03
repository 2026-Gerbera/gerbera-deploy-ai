"""source=fixture: 결정12의 단일 내용 판정과 개발자 직접 수정 회귀."""

import json

import pytest

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.patch_config import MAX_PATCH_CHARS
from ddak.core.patch_ledger import file_diff
from ddak.plan.patch.check import build_patch
from ddak.plan.patch.history import PreviousFile, lost_violations, previous_files

COOKIE_OLD = 'import os\nSESSION_COOKIE_SECURE = False\nHOST = "example.test"\n'
COOKIE_FIXED = (
    'import os\nSESSION_COOKIE_SECURE = (os.environ["SESSION_COOKIE_SECURE"].lower() == "true")\n'
    'HOST = "example.test"\n'
)
CASES = [
    (COOKIE_OLD, COOKIE_FIXED),
    (
        'import os\nSECRET_KEY = "' + 'dev"\n',
        'import os\nSECRET_KEY = os.environ["SECRET_KEY"]\n',
    ),
    ('import os\nHOST = "localhost"\n', 'import os\nHOST = os.environ["HOST"]\n'),
    (
        "import os\napp.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)\n",
        "import os\napp.wsgi_app = ProxyFix(app.wsgi_app, "
        'x_proto=int(os.environ["PROXY_FIX_X_PROTO"]))\n',
    ),
]


def history(old, fixed, name="app.py"):
    return previous_files(file_diff(name, old.encode(), fixed.encode()))


@pytest.mark.parametrize("renderer", ["ledger", "checker"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize(
    ("old_eof", "new_eof"), [(True, True), (True, False), (False, True), (False, False)]
)
@pytest.mark.parametrize("context_tail", [False, True])
def test_previous_files_roundtrip_preserves_bytes_and_missing_final_newline(
    renderer, newline, old_eof, new_eof, context_tail
):
    old = "import os\nHOST = 'localhost'\n"
    new = 'import os\nHOST = os.environ["HOST"]\n'
    if context_tail:
        old += "VERSION = 1\n"
        new += "VERSION = 1\n"
    old = old.replace("\n", newline)
    new = new.replace("\n", newline)
    if not old_eof:
        old = old.removesuffix(newline)
    if not new_eof:
        new = new.removesuffix(newline)
    patch = (
        file_diff("app.py", old.encode(), new.encode())
        if renderer == "ledger"
        else build_patch({"app.py": (old, new)})
    )
    restored = previous_files(patch)
    assert set(restored) == {"app.py"}
    assert restored["app.py"].old.encode() == old.encode()
    assert restored["app.py"].new.encode() == new.encode()
    removed = ("HOST = 'localhost'",)
    # 줄바꿈 자체도 바뀐 경우, 동일한 마지막 문맥 줄이 diff의 삭제 줄로 기록된다.
    if context_tail and old_eof != new_eof:
        removed += ("VERSION = 1",)
    assert restored["app.py"].removed == removed


VALID = file_diff("app.py", COOKIE_OLD.encode(), COOKIE_FIXED.encode())


@pytest.mark.parametrize(
    "patch",
    [
        b"",
        b"\xff",
        b"not a diff\n",
        VALID.rsplit(b"\n", 2)[0] + b"\n",
        VALID.replace(b"@@ -1,3 +1,3 @@", b"@@ -1,9 +1,3 @@"),
        VALID.replace(b"@@ -1,3 +1,3 @@", b"@@ -2,3 +2,3 @@"),
        VALID.replace(b"+++ b/app.py", b"+++ b/other.py"),
        VALID.replace(b"a/app.py", b"a/../app.py").replace(b"b/app.py", b"b/../app.py"),
        VALID.replace(b"--- a/app.py", b"--- /dev/null"),
        VALID.replace(b"+++ b/app.py", b"+++ /dev/null"),
        VALID + VALID,
        VALID + b"@@ -1 +1 @@\n-a\n+b\n",
        VALID + b"unexpected trailing data\n",
        b"\\ No newline at end of file\n" + VALID,
        b" " * (MAX_PATCH_CHARS + 1),
    ],
    ids=[
        "empty",
        "non-utf8",
        "not-diff",
        "truncated",
        "wrong-count",
        "partial-file",
        "rename",
        "traversal",
        "created-file",
        "deleted-file",
        "duplicate-file",
        "second-hunk",
        "trailing-data",
        "misplaced-eof-marker",
        "oversized",
    ],
)
def test_corrupted_previous_diff_is_rejected_instead_of_empty_history(patch):
    with pytest.raises(DdakToolError) as caught:
        previous_files(patch)
    assert caught.value.code is ErrorCode.PRECONDITION_FAILED
    assert "SESSION_COOKIE_SECURE" not in str(caught.value)


@pytest.mark.parametrize(("old", "fixed"), CASES)
def test_upstream_preserves_fixed_expression_after_version_and_line_movement(old, fixed):
    final = fixed.replace("import os\n", "import os\nVERSION = 2\n\n")
    assert lost_violations(history(old, fixed), {"app.py": final}) == ()


@pytest.mark.parametrize(("old", "fixed"), CASES)
def test_unchanged_original_is_lost_but_exact_fixed_result_is_not(old, fixed):
    previous = history(old, fixed)
    violations = lost_violations(previous, {"app.py": old})
    assert [(v.code, v.file, v.line) for v in violations] == [("patch_lost", "app.py", 2)]
    assert lost_violations(previous, {"app.py": fixed}) == ()


@pytest.mark.parametrize(("old", "fixed"), CASES)
def test_reappeared_value_line_reports_current_line_and_redacts_value(old, fixed):
    previous = history(old, fixed)
    removed = old.splitlines()[1]
    final = "# current source\nimport os\nVERSION = 2\n\n    " + removed + "\n"
    violations = lost_violations(previous, {"app.py": final})
    assert isinstance(violations, tuple)
    assert [v.model_dump(mode="json") for v in violations] == [
        {"code": "patch_lost", "file": "app.py", "line": 5}
    ]
    payload = json.dumps([v.model_dump(mode="json") for v in violations])
    assert removed not in payload and removed not in repr(violations)
    assert all(set(v.model_dump()) == {"code", "file", "line"} for v in violations)


@pytest.mark.parametrize(
    "final",
    [{}, {"app.py": ""}, {"app.py": "import os\nVERSION = 2\n"}],
    ids=["deleted-file", "emptied-file", "deleted-value-line"],
)
def test_developer_deleting_file_or_value_line_is_not_loss(final):
    assert lost_violations(history(COOKIE_OLD, COOKIE_FIXED), final) == ()


def test_cookie_value_reappearance_is_not_hidden_by_other_address_patch():
    old = COOKIE_OLD.replace('"example.test"', '"localhost"')
    fixed = COOKIE_FIXED.replace('"example.test"', 'os.environ["HOST"]')
    final = 'import os\nSESSION_COOKIE_SECURE = False\nHOST = os.environ["HOST"]\n'
    assert [(v.file, v.line) for v in lost_violations(history(old, fixed), {"app.py": final})] == [
        ("app.py", 2)
    ]


def test_same_original_value_is_lost_even_when_fixed_expression_is_also_present():
    final = COOKIE_FIXED + "SESSION_COOKIE_SECURE = False\n"
    violations = lost_violations(history(COOKIE_OLD, COOKIE_FIXED), {"app.py": final})
    assert [(v.file, v.line) for v in violations] == [("app.py", 4)]


@pytest.mark.parametrize(
    "replacement",
    [
        'OTHER = (os.environ["SESSION_COOKIE_SECURE"].lower() == "true")\n'
        "SESSION_COOKIE_SECURE = bool(0)",
        'SESSION_COOKIE_SECURE = (os.environ["SESSION_COOKIE_SECURE"].lower() == "true")\n'
        "SESSION_COOKIE_SECURE = bool(0)",
        "if False:\n    SESSION_COOKIE_SECURE = "
        '(os.environ["SESSION_COOKIE_SECURE"].lower() == "true")',
        'SESSION_COOKIE_SECURE = (os.environ["SESSION_COOKIE_SECURE"].lower() == "true") and False',
        "SESSION_COOKIE_SECURE = bool(0)",
    ],
)
def test_developer_changed_expression_is_allowed_without_semantic_owner_obligation(replacement):
    final = "import os\n" + replacement + '\nHOST = os.environ["HOST"]\n'
    assert lost_violations(history(COOKIE_OLD, COOKIE_FIXED), {"app.py": final}) == ()


def test_developer_can_move_expression_to_another_function():
    old = 'import os\ndef configure(app):\n    app.config["SESSION_COOKIE_SECURE"] = False\n'
    fixed = old.replace("False", '(os.environ["SESSION_COOKIE_SECURE"].lower() == "true")')
    final = fixed.replace("def configure(app):", "def unused(app):")
    assert lost_violations(history(old, fixed), {"app.py": final}) == ()


@pytest.mark.parametrize("binding", ["os = object()\n", "def unused(os):\n    pass\n"])
def test_developer_os_binding_change_is_not_historical_patch_loss(binding):
    final = COOKIE_FIXED + binding + "VERSION = 2\n"
    assert lost_violations(history(COOKIE_OLD, COOKIE_FIXED), {"app.py": final}) == ()


def test_developer_conversion_binding_change_is_not_historical_patch_loss():
    old, fixed = CASES[3]
    assert lost_violations(history(old, fixed), {"app.py": fixed + "int = lambda value: 0\n"}) == ()


def test_developer_quote_and_whitespace_changes_are_allowed():
    final = COOKIE_FIXED.replace('"SESSION_COOKIE_SECURE"', "'SESSION_COOKIE_SECURE'")
    final = final.replace("SESSION_COOKIE_SECURE =", "SESSION_COOKIE_SECURE  =")
    assert lost_violations(history(COOKIE_OLD, COOKIE_FIXED), {"app.py": final}) == ()


def test_unpatched_existing_env_expression_can_change_upstream():
    existing = 'A = os.environ["A"]\n'
    previous = history(COOKIE_OLD + existing, COOKIE_FIXED + existing)
    final = COOKIE_FIXED + 'A = os.environ["A"].strip()\nVERSION = 2\n'
    assert lost_violations(previous, {"app.py": final}) == ()


def test_only_removed_target_lines_are_loss_candidates():
    old = "VERSION = 1\nSESSION_COOKIE_SECURE = False\nHOST = 'localhost'\n"
    fixed = "VERSION = 2\nSESSION_COOKIE_SECURE = False\nHOST = os.environ['HOST']\n"
    previous = history(old, fixed)
    # 대상이 아닌 삭제 줄과 변경하지 않은 대상 문맥 줄은 그대로 있어도 손실이 아니다.
    final = "VERSION = 1\nSESSION_COOKIE_SECURE = False\nHOST = os.environ['HOST']\n"
    assert lost_violations(previous, {"app.py": final}) == ()


def test_multiple_file_history_restores_each_file_and_reports_sorted_current_locations():
    old, fixed = CASES[2]
    patch = file_diff("z.py", old.encode(), fixed.encode()) + file_diff(
        "a.py", COOKIE_OLD.encode(), COOKIE_FIXED.encode()
    )
    previous = previous_files(patch)
    assert previous["z.py"] == PreviousFile(old, fixed, ('HOST = "localhost"',))
    assert previous["a.py"].old == COOKIE_OLD and previous["a.py"].new == COOKIE_FIXED
    final = {"z.py": "# new line\n" + old, "a.py": COOKIE_OLD}
    assert [(v.file, v.line) for v in lost_violations(previous, final)] == [
        ("a.py", 2),
        ("z.py", 3),
    ]


def test_each_duplicate_reappearance_has_current_location_and_output_is_bounded():
    previous = history(COOKIE_OLD, COOKIE_FIXED)
    final = "# current source\n" + "SESSION_COOKIE_SECURE = False\n" * 60
    violations = lost_violations(previous, {"app.py": final})
    assert len(violations) == 50
    assert [(v.file, v.line) for v in violations] == [("app.py", n) for n in range(2, 52)]
