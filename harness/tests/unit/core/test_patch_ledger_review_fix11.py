"""source=fixture: 결정12 툴 판정 연결·승인 해시·원장 호환 회귀."""

import json

import pytest

from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.tools.patch_config import PatchConfigOutput, PatchMeta, PatchViolation
from ddak.core.patch_ledger import (
    approved_patches,
    file_diff,
    guard_patch_loss,
    reuse_patches,
    save_ledger,
)
from ddak.core.snapshots import digest_bytes

RUN_ID = "run-current"
PATCH = file_diff(
    "app.py",
    b"import os\nSESSION_COOKIE_SECURE = False\n",
    b'import os\nSESSION_COOKIE_SECURE = (os.environ["SESSION_COOKIE_SECURE"] == "true")\n',
)


def review(status, **changes):
    accepted = status in {"proposed", "reused"}
    return PatchConfigOutput(
        **{
            "run_id": RUN_ID,
            "status": status,
            "passed": accepted,
            "patch": PATCH.decode() if accepted else None,
            "patch_sha256": digest_bytes(PATCH) if accepted else None,
            "meta": (
                PatchMeta(reason="fixture", reuse=status == "reused", source="fixture")
                if accepted
                else None
            ),
            **changes,
        }
    )


@pytest.mark.parametrize("status", ["no_targets", "rejected"])
def test_ledger_follows_tool_verdict_without_patch_even_with_previous(status):
    guard_patch_loss(review(status), run_id=RUN_ID, patch=None, has_previous=True)


@pytest.mark.parametrize("status", ["proposed", "reused"])
def test_ledger_follows_accepted_tool_verdict_with_exact_patch_hash(status):
    guard_patch_loss(review(status), run_id=RUN_ID, patch=PATCH, has_previous=True)


def test_ledger_stops_on_patch_lost_with_only_current_locations():
    out = review(
        "patch_lost",
        violations=[PatchViolation(code="patch_lost", file="app.py", line=7)],
    )
    with pytest.raises(DdakToolError, match="patch_lost") as caught:
        guard_patch_loss(out, run_id=RUN_ID, patch=None, has_previous=True)
    message = str(caught.value)
    assert "app.py:7" in message and "값 가림" in message
    assert "SESSION_COOKIE_SECURE" not in message and "False" not in message


def test_ledger_stops_on_patch_lost_without_violation_details():
    with pytest.raises(DdakToolError, match="patch_lost"):
        guard_patch_loss(review("patch_lost"), run_id=RUN_ID, patch=None, has_previous=True)


@pytest.mark.parametrize("patch", [None, b""])
def test_previous_without_patch_requires_tool_verdict(patch):
    with pytest.raises(DdakToolError, match="툴 판정"):
        guard_patch_loss(None, run_id=RUN_ID, patch=patch, has_previous=True)


@pytest.mark.parametrize(("has_previous", "patch"), [(False, None), (False, PATCH)])
def test_missing_verdict_preserves_existing_patch_connection(has_previous, patch):
    guard_patch_loss(None, run_id=RUN_ID, patch=patch, has_previous=has_previous)


@pytest.mark.parametrize("status", ["no_targets", "rejected", "proposed", "reused", "patch_lost"])
def test_tool_verdict_for_other_run_is_rejected(status):
    with pytest.raises(DdakToolError, match="run ID"):
        guard_patch_loss(
            review(status, run_id="run-other"),
            run_id=RUN_ID,
            patch=PATCH if status in {"proposed", "reused"} else None,
            has_previous=True,
        )


@pytest.mark.parametrize("status", ["proposed", "reused"])
@pytest.mark.parametrize(
    "changes",
    [
        {"passed": False},
        {"patch_sha256": None},
        {"patch_sha256": digest_bytes(b"different patch")},
        {"patch": None},
        {"patch": PATCH.decode() + "\n"},
    ],
    ids=["not-passed", "missing-hash", "wrong-hash", "missing-text", "wrong-text"],
)
def test_accepted_verdict_must_match_passed_hash_and_patch_text(status, changes):
    with pytest.raises(DdakToolError, match="승인 대상"):
        guard_patch_loss(review(status, **changes), run_id=RUN_ID, patch=PATCH)


@pytest.mark.parametrize("status", ["proposed", "reused"])
@pytest.mark.parametrize("patch", [None, b"", PATCH + b"\n"])
def test_accepted_verdict_requires_matching_nonempty_approval_bytes(status, patch):
    with pytest.raises(DdakToolError, match="승인 대상"):
        guard_patch_loss(review(status), run_id=RUN_ID, patch=patch)


@pytest.mark.parametrize("status", ["no_targets", "rejected"])
@pytest.mark.parametrize("mismatch", ["passed", "review-patch", "approval-patch"])
def test_nonaccepted_verdict_cannot_carry_approval(status, mismatch):
    changes = {"passed": True} if mismatch == "passed" else {}
    if mismatch == "review-patch":
        changes["patch"] = PATCH.decode()
    with pytest.raises(DdakToolError, match="승인 대상"):
        guard_patch_loss(
            review(status, **changes),
            run_id=RUN_ID,
            patch=PATCH if mismatch == "approval-patch" else None,
            has_previous=True,
        )


def history(tmp_path, old, fixed):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text(old)
    directory = tmp_path / "runs" / "first"
    directory.mkdir(parents=True)
    patch = file_diff("app.py", old.encode(), fixed.encode())
    entries = save_ledger(directory, source, patch)
    return source, {"local": {"release_id": "first", "patch_ledger": entries}}, patch


def test_ledger_keeps_old_hashes_and_contains_only_hashed_semantic_evidence(tmp_path):
    sentinel = "fixture-" + "private-original-value"
    old = f'import os\nSECRET_KEY = "{sentinel}"\n'
    fixed = 'import os\nSECRET_KEY = os.environ["SECRET_KEY"]\n'
    _, previous, patch = history(tmp_path, old, fixed)
    entry = previous["local"]["patch_ledger"]["app.py"]
    assert entry["source_sha256"] == digest_bytes(old.encode())
    assert entry["result_sha256"] == digest_bytes(fixed.encode())
    assert entry["patch_sha256"] == digest_bytes(patch)
    assert entry["removed_lines"] == [digest_bytes(old.splitlines()[1].encode())]
    evidence = entry["required_env_expressions"]
    assert evidence and all(key.startswith("sha256:") for key in evidence)
    assert all(value.startswith("sha256:") for values in evidence.values() for value in values)
    assert sentinel not in json.dumps(previous)
    assert "os.environ" not in json.dumps(evidence)


@pytest.mark.parametrize("evidence", [None, {}, [], {"bad": ["bad"]}])
def test_legacy_semantic_evidence_is_metadata_without_content_veto(tmp_path, evidence):
    old = "import os\nSESSION_COOKIE_SECURE = False\n"
    fixed = 'import os\nSESSION_COOKIE_SECURE = os.environ["SESSION_COOKIE_SECURE"]\n'
    source, previous, patch = history(tmp_path, old, fixed)
    previous["local"]["patch_ledger"]["app.py"]["required_env_expressions"] = evidence
    assert reuse_patches(source, previous, tmp_path / "runs") == (patch, [])
    (source / "app.py").write_text(fixed + "VERSION = 2\n")
    assert approved_patches(previous, tmp_path / "runs") == {"app.py": patch}
    assert reuse_patches(source, previous, tmp_path / "runs") == (None, ["app.py"])
    guard_patch_loss(review("no_targets"), run_id=RUN_ID, patch=None, has_previous=True)


def test_missing_legacy_semantic_evidence_does_not_require_exact_result(tmp_path):
    old = "import os\nSESSION_COOKIE_SECURE = False\n"
    fixed = 'import os\nSESSION_COOKIE_SECURE = os.environ["SESSION_COOKIE_SECURE"]\n'
    source, previous, patch = history(tmp_path, old, fixed)
    previous["local"]["patch_ledger"]["app.py"].pop("required_env_expressions", None)
    assert reuse_patches(source, previous, tmp_path / "runs") == (patch, [])
    (source / "app.py").write_text(fixed + "VERSION = 2\n")
    assert approved_patches(previous, tmp_path / "runs") == {"app.py": patch}
    assert reuse_patches(source, previous, tmp_path / "runs") == (None, ["app.py"])
    guard_patch_loss(review("no_targets"), run_id=RUN_ID, patch=None, has_previous=True)


def test_previous_with_partial_patch_still_requires_tool_verdict():
    with pytest.raises(DdakToolError, match="툴 판정"):
        guard_patch_loss(None, run_id=RUN_ID, patch=PATCH, has_previous=True)
