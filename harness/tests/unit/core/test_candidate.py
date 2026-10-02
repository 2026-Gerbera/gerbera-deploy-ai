"""로컬 Git 후보 경로. fixture 검사와 선택적인 실제 CLI 검사를 구분한다."""

import hashlib
import shutil
import subprocess

import pytest

from ddak.core.candidate import scan_staged, tree_manifest
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.snapshots import file_manifest, materialize, preview
from tests.unit.core import test_app_repository as shared

git = shared.git
repository = shared.repository

PATCH = b"--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-version = 1\n+version = 2\n"


@pytest.fixture(autouse=True)
def operator_identity(monkeypatch):
    # 테스트 프로세스의 Git 사용자 설정. author/committer 환경변수나 저장소 config 수정 없음.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    for index, key, value in [
        (0, "user.name", "Local Operator"),
        (1, "user.email", "operator@example.test"),
    ]:
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)


def approved(tmp_path, extra=False):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("version = 1\n")
    if extra:
        (source / "README").write_text("New feature\n")
    build = tmp_path / "build"
    materialize(source, build, preview(source, PATCH), PATCH)
    return file_manifest(source), file_manifest(build)


def advance_prod(repo):
    git(repo.path, "switch", "-c", "prod", "origin/prod")
    (repo.path / "README").write_text("New feature\n")
    git(repo.path, "add", "README")
    git(repo.path, "commit", "-m", "Product update")
    git(repo.path, "push", "origin", "prod")
    return git(repo.path, "rev-parse", "HEAD")


def test_reuse_existing_approved_candidate(repository, tmp_path):
    repo, bare, v1, v2 = repository
    scans, guards = [], []
    repo.secret_scan = lambda path: scans.append(path)
    original, patched = approved(tmp_path)
    result = repo.prepare_candidate(
        v1, original, patched, PATCH, tmp_path / "candidate", lambda: guards.append(True)
    )
    assert result["candidate_sha"] != v2 and result["reused"] is False
    assert git(repo.path, "show", "-s", "--format=%P", result["candidate_sha"]).split() == [v2, v1]
    assert scans and len(guards) >= 2
    assert git(bare, "rev-parse", "ai-prod") == result["candidate_sha"]
    assert git(bare, "rev-parse", "main") == v1


@pytest.mark.parametrize("real_scanner", [False, True])
def test_merge_preserves_ai_history_and_exact_approved_tree(repository, tmp_path, real_scanner):
    if real_scanner and not shutil.which("gitleaks"):
        pytest.skip("실제 비밀검사 경로는 Gitleaks 설치 필요")
    repo, bare, v1, v2 = repository
    source_sha = advance_prod(repo)
    original, patched = approved(tmp_path, extra=True)
    repo.secret_scan = None if real_scanner else lambda path: None
    result = repo.prepare_candidate(
        source_sha, original, patched, PATCH, tmp_path / "candidate", lambda: None
    )
    sha = result["candidate_sha"]
    git(repo.path, "merge-base", "--is-ancestor", v2, sha)
    git(repo.path, "merge-base", "--is-ancestor", source_sha, sha)
    assert tree_manifest(repo, sha) == patched
    assert (
        git(repo.path, "show", "-s", "--format=%an <%ae>", sha)
        == "Local Operator <operator@example.test>"
    )
    assert git(bare, "rev-parse", "ai-prod") == sha
    assert git(bare, "rev-parse", "main") == v1


@pytest.mark.skipif(not shutil.which("gitleaks"), reason="Gitleaks 설치 필요")
def test_real_scanner_rejects_synthetic_token_despite_app_allow_config(tmp_path):
    (tmp_path / "app.py").write_text("VERSION = 1\n")
    scan_staged(tmp_path)
    # 실제 자격증명이 아닌, 형식/엔트로피 탐지용 고정 가짜 문자열이다.
    token = "gh" + "p_" + hashlib.sha256(b"ddak synthetic fixture").hexdigest()[:36]
    (tmp_path / "app.py").write_text("TOKEN = " + repr(token) + " # gitleaks:allow\n")
    (tmp_path / ".gitleaks.toml").write_text('[allowlist]\npaths = [".*"]\n')
    with pytest.raises(DdakToolError, match="비밀값 검사 실패"):
        scan_staged(tmp_path)


def test_new_patch_is_committed_only_after_scanner(repository, tmp_path):
    repo, bare, v1, v2 = repository
    # ai-prod가 아직 없었던 첫 후보: 삭제는 테스트 전용 bare fixture에서만 한다.
    git(bare, "update-ref", "-d", "refs/heads/ai-prod", v2)
    original, patched = approved(tmp_path)
    heads = []
    repo.secret_scan = lambda path: heads.append(git(path, "rev-parse", "HEAD"))
    result = repo.prepare_candidate(
        v1, original, patched, PATCH, tmp_path / "candidate", lambda: None
    )
    assert heads[0] == heads[-1] == v1
    assert git(bare, "rev-parse", "ai-prod") == result["candidate_sha"]
    assert tree_manifest(repo, result["candidate_sha"]) == patched


def test_secret_scan_failure_blocks_commit_and_push(repository, tmp_path):
    repo, bare, _v1, v2 = repository
    source_sha = advance_prod(repo)
    original, patched = approved(tmp_path, extra=True)

    def reject(path):
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "fixture secret rejected")

    repo.secret_scan = reject
    workspace = tmp_path / "candidate"
    with pytest.raises(DdakToolError, match="secret rejected"):
        repo.prepare_candidate(source_sha, original, patched, PATCH, workspace, lambda: None)
    assert git(workspace, "rev-parse", "HEAD") == v2
    assert git(bare, "rev-parse", "ai-prod") == v2


@pytest.mark.parametrize("scenario", ["new_hunk", "no_patch", "conflict", "failed_run_candidate"])
def test_approved_tree_replaces_previous_candidate(repository, tmp_path, scenario):
    repo, bare, v1, v2 = repository
    repo.secret_scan = lambda path: None
    git(repo.path, "switch", "-c", "prod", "origin/prod")
    (repo.path / "new.py").write_text("new = 1\n")
    if scenario == "conflict":
        (repo.path / "app.py").write_text("version = 3\n")
    git(repo.path, "add", "--all")
    git(repo.path, "commit", "-m", "Next product revision")
    prod = git(repo.path, "rev-parse", "HEAD")
    git(repo.path, "push", "origin", "prod")
    source = file_manifest(repo.path)
    approved_tree = tmp_path / "approved"
    approved_tree.mkdir()
    (approved_tree / "app.py").write_text(
        "version = 1\n"
        if scenario == "no_patch"
        else "version = 4\n"
        if scenario == "conflict"
        else "version = 2\n"
    )
    (approved_tree / "new.py").write_text("new = 1\n" if scenario == "no_patch" else "new = 2\n")
    parent = v2
    if scenario == "failed_run_candidate":
        abandoned = repo.prepare_candidate(
            prod, source, source, None, tmp_path / "failed", lambda: None
        )
        parent = abandoned["candidate_sha"]  # 빌드/배포 실패로 main은 그대로인 후보
    result = repo.prepare_candidate(
        prod,
        source,
        file_manifest(approved_tree),
        None,
        tmp_path / "candidate",
        lambda: None,
        approved_tree,
    )
    sha = result["candidate_sha"]
    assert tree_manifest(repo, sha) == file_manifest(approved_tree)
    assert git(repo.path, "show", "-s", "--format=%P", sha).split() == [parent, prod]
    assert git(bare, "rev-parse", "ai-prod") == sha
    assert git(bare, "rev-parse", "main") == v1
    assert ("app.py" in result["merge_conflicts"]) == (scenario == "conflict")


@pytest.mark.parametrize("name", [".env.example", ".env.sample"])
def test_templates_follow_prod_and_are_scanned(repository, tmp_path, name):
    repo, bare, v1, _v2 = repository
    git(repo.path, "switch", "-c", "prod", "origin/prod")
    (repo.path / name).write_text("APP_ENV=production\n")
    git(repo.path, "add", name)
    git(repo.path, "commit", "-m", "Add environment template")
    prod = git(repo.path, "rev-parse", "HEAD")
    git(repo.path, "push", "origin", "prod")
    scans = []
    repo.secret_scan = lambda path: scans.append((path / name).read_text())
    managed = file_manifest(repo.path)
    result = repo.prepare_candidate(
        prod, managed, managed, None, tmp_path / "candidate", lambda: None
    )
    assert name not in tree_manifest(repo, result["candidate_sha"])
    assert git(bare, "show", result["candidate_sha"] + ":" + name) == "APP_ENV=production"
    assert scans and all(s == "APP_ENV=production\n" for s in scans)
    assert git(bare, "rev-parse", "main") == v1


@pytest.mark.parametrize("name", [".env", "private.pem", "private.key"])
def test_sensitive_tracked_files_still_fail_closed(repository, tmp_path, name):
    repo, _bare, _v1, _v2 = repository
    (repo.path / name).write_text("fixture-only\n")
    git(repo.path, "add", name)
    git(repo.path, "commit", "-m", "Unsupported fixture")
    with pytest.raises(DdakToolError, match="지원하지 않는 파일"):
        tree_manifest(repo, git(repo.path, "rev-parse", "HEAD"))


def test_missing_scanner_fails_closed(tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(DdakToolError, match="검사 도구 실행 실패"):
        scan_staged(tmp_path)


def test_approval_revoked_before_commit_does_not_push(repository, tmp_path):
    repo, bare, _v1, v2 = repository
    source_sha = advance_prod(repo)
    original, patched = approved(tmp_path, extra=True)
    repo.secret_scan = lambda path: None
    count = 0

    def guard():
        nonlocal count
        count += 1
        if count == 2:
            raise DdakToolError(ErrorCode.APPROVAL_REQUIRED, "approval changed")

    with pytest.raises(DdakToolError, match="approval changed"):
        repo.prepare_candidate(source_sha, original, patched, PATCH, tmp_path / "candidate", guard)
    assert git(bare, "rev-parse", "ai-prod") == v2


def test_staged_mutation_by_scanner_is_rejected(repository, tmp_path):
    repo, bare, _v1, v2 = repository
    source_sha = advance_prod(repo)
    original, patched = approved(tmp_path, extra=True)

    def mutate(path):
        (path / "extra.py").write_text("unapproved = True\n")
        git(path, "add", "extra.py")

    repo.secret_scan = mutate
    with pytest.raises(DdakToolError, match="staged 트리가 변경"):
        repo.prepare_candidate(
            source_sha, original, patched, PATCH, tmp_path / "candidate", lambda: None
        )
    assert git(bare, "rev-parse", "ai-prod") == v2


def test_supplied_candidate_also_requires_secret_scan(repository, tmp_path):
    repo, _bare, v1, v2 = repository
    original, patched = approved(tmp_path)

    def reject(path):
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "scanner required")

    repo.secret_scan = reject
    with pytest.raises(DdakToolError, match="scanner required"):
        repo.validate_candidate(v1, v2, original, patched, tmp_path / "check", lambda: None)


def test_push_uses_verified_oid_even_if_head_moves(repository, tmp_path):
    repo, bare, v1, v2 = repository
    original, patched = approved(tmp_path)

    def move_head(path):
        git(path, "commit", "--allow-empty", "-m", "Unrelated local commit")

    repo.secret_scan = move_head
    result = repo.prepare_candidate(
        v1, original, patched, PATCH, tmp_path / "candidate", lambda: None
    )
    assert git(tmp_path / "candidate", "rev-parse", "HEAD") != v2
    assert result["candidate_sha"] == git(bare, "rev-parse", "ai-prod")
    assert result["candidate_sha"] != git(tmp_path / "candidate", "rev-parse", "HEAD")


def test_approved_ignored_file_is_staged(repository, tmp_path):
    repo, bare, v1, _v2 = repository
    repo.secret_scan = lambda path: None
    source, _patched = approved(tmp_path)
    build = tmp_path / "build"
    (build / ".gitignore").write_text("generated.py\n")
    (build / "generated.py").write_text("approved = True\n")
    result = repo.prepare_candidate(
        v1, source, file_manifest(build), None, tmp_path / "candidate", lambda: None, build
    )
    assert git(bare, "show", result["candidate_sha"] + ":generated.py") == "approved = True"
