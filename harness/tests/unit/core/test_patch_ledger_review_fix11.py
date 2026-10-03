"""source=fixture: 승인한 환경변수 표현식의 손실과 정상 upstream 변경 회귀."""

import json

import pytest

from ddak.core.contracts.errors import DdakToolError
from ddak.core.patch_ledger import file_diff, guard_patch_loss, save_ledger
from ddak.core.snapshots import copy_source, digest_bytes


def history(tmp_path, old, fixed):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text(old)
    directory = tmp_path / "runs" / "first"
    directory.mkdir(parents=True)
    entries = save_ledger(directory, source, file_diff("app.py", old.encode(), fixed.encode()))
    return source, {"local": {"release_id": "first", "patch_ledger": entries}}


def changed_tree(tmp_path, source, upstream, approved=None):
    (source / "app.py").write_text(upstream)
    built = tmp_path / "built"
    copy_source(source, built)
    if approved is not None:
        (built / "app.py").write_text(approved)
    return built


COOKIE_OLD = 'import os\nSESSION_COOKIE_SECURE = False\nHOST = "example.test"\n'
COOKIE_FIXED = (
    'import os\nSESSION_COOKIE_SECURE = (os.environ["SESSION_COOKIE_SECURE"].lower() == "true")\n'
    'HOST = "example.test"\n'
)


@pytest.mark.parametrize(
    ("old", "fixed"),
    [
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
    ],
)
def test_upstream_preserves_expression_with_version_and_line_movement(tmp_path, old, fixed):
    source, previous = history(tmp_path, old, fixed)
    upstream = fixed.replace("import os\n", "import os\nVERSION = 2\n\n")
    built = changed_tree(tmp_path, source, upstream)
    guard_patch_loss(source, built, previous)


def test_unpatched_existing_env_expression_can_change_upstream(tmp_path):
    existing = 'A = os.environ["A"]\n'
    source, previous = history(tmp_path, COOKIE_OLD + existing, COOKIE_FIXED + existing)
    upstream = COOKIE_FIXED + 'A = os.environ["A"].strip()\nVERSION = 2\n'
    built = changed_tree(tmp_path, source, upstream)
    guard_patch_loss(source, built, previous)


def test_cookie_loss_hidden_by_other_address_patch_is_rejected(tmp_path):
    source, previous = history(tmp_path, COOKIE_OLD, COOKIE_FIXED)
    upstream = 'import os\nSESSION_COOKIE_SECURE = bool(0)\nHOST = "localhost"\n'
    approved = upstream.replace('"localhost"', 'os.environ["HOST"]')
    built = changed_tree(tmp_path, source, upstream, approved)
    with pytest.raises(DdakToolError, match="손실"):
        guard_patch_loss(source, built, previous)


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
    ],
)
def test_same_env_read_in_decoy_or_changed_expression_cannot_satisfy_obligation(
    tmp_path, replacement
):
    source, previous = history(tmp_path, COOKIE_OLD, COOKIE_FIXED)
    upstream = "import os\n" + replacement + '\nHOST = "localhost"\n'
    approved = upstream.replace('"localhost"', 'os.environ["HOST"]')
    built = changed_tree(tmp_path, source, upstream, approved)
    with pytest.raises(DdakToolError, match="손실"):
        guard_patch_loss(source, built, previous)


def test_expression_in_other_function_is_not_same_semantic_owner(tmp_path):
    old = 'import os\ndef configure(app):\n    app.config["SESSION_COOKIE_SECURE"] = False\n'
    fixed = old.replace("False", '(os.environ["SESSION_COOKIE_SECURE"].lower() == "true")')
    source, previous = history(tmp_path, old, fixed)
    upstream = fixed.replace("def configure(app):", "def unused(app):") + 'HOST = "localhost"\n'
    built = changed_tree(
        tmp_path, source, upstream, upstream.replace('"localhost"', 'os.environ["HOST"]')
    )
    with pytest.raises(DdakToolError, match="손실"):
        guard_patch_loss(source, built, previous)


@pytest.mark.parametrize("binding", ["os = object()\n", "def unused(os):\n    pass\n"])
def test_rebound_os_cannot_satisfy_same_expression_hash(tmp_path, binding):
    source, previous = history(tmp_path, COOKIE_OLD, COOKIE_FIXED)
    upstream = COOKIE_FIXED + binding + "VERSION = 2\n"
    built = changed_tree(tmp_path, source, upstream)
    with pytest.raises(DdakToolError, match="손실"):
        guard_patch_loss(source, built, previous)


def test_rebound_conversion_cannot_satisfy_same_expression_hash(tmp_path):
    old = "import os\napp.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)\n"
    fixed = old.replace("x_proto=1", 'x_proto=int(os.environ["PROXY_FIX_X_PROTO"])')
    source, previous = history(tmp_path, old, fixed)
    upstream = fixed + "int = lambda value: 0\n"
    built = changed_tree(tmp_path, source, upstream)
    with pytest.raises(DdakToolError, match="손실"):
        guard_patch_loss(source, built, previous)


def test_quote_and_whitespace_changes_preserve_semantic_expression(tmp_path):
    source, previous = history(tmp_path, COOKIE_OLD, COOKIE_FIXED)
    upstream = (
        COOKIE_FIXED.replace('"SESSION_COOKIE_SECURE"', "'SESSION_COOKIE_SECURE'") + "VERSION = 2\n"
    )
    built = changed_tree(tmp_path, source, upstream)
    guard_patch_loss(source, built, previous)


def test_ledger_keeps_old_hashes_and_contains_only_hashed_semantic_evidence(tmp_path):
    sentinel = "fixture-" + "private-original-value"
    old = f'import os\nSECRET_KEY = "{sentinel}"\n'
    fixed = 'import os\nSECRET_KEY = os.environ["SECRET_KEY"]\n'
    _, previous = history(tmp_path, old, fixed)
    entry = previous["local"]["patch_ledger"]["app.py"]
    assert entry["source_sha256"] == digest_bytes(old.encode())
    assert entry["result_sha256"] == digest_bytes(fixed.encode())
    assert entry["removed_lines"] == [digest_bytes(old.splitlines()[1].encode())]
    evidence = entry["required_env_expressions"]
    assert evidence and all(key.startswith("sha256:") for key in evidence)
    assert all(value.startswith("sha256:") for values in evidence.values() for value in values)
    assert sentinel not in json.dumps(previous)
    assert "os.environ" not in json.dumps(evidence)


@pytest.mark.parametrize("exact", [True, False])
def test_legacy_semantic_evidence_missing_allows_only_exact_result(tmp_path, exact):
    source, previous = history(tmp_path, COOKIE_OLD, COOKIE_FIXED)
    previous["local"]["patch_ledger"]["app.py"].pop("required_env_expressions", None)
    upstream = COOKIE_FIXED if exact else COOKIE_FIXED + "VERSION = 2\n"
    built = changed_tree(tmp_path, source, upstream)
    if exact:
        guard_patch_loss(source, built, previous)
    else:
        with pytest.raises(DdakToolError, match="손실"):
            guard_patch_loss(source, built, previous)


@pytest.mark.parametrize("evidence", [None, {}, [], {"bad": ["bad"]}])
def test_missing_or_malformed_semantic_evidence_is_fail_closed(tmp_path, evidence):
    source, previous = history(tmp_path, COOKIE_OLD, COOKIE_FIXED)
    previous["local"]["patch_ledger"]["app.py"]["required_env_expressions"] = evidence
    upstream = COOKIE_FIXED + "VERSION = 2\n"
    built = changed_tree(tmp_path, source, upstream)
    with pytest.raises(DdakToolError, match="손실"):
        guard_patch_loss(source, built, previous)
