"""로컬 Git 후보 경로. fixture 검사와 선택적인 실제 CLI 검사를 구분한다."""

import hashlib
import json
import shutil
import subprocess

import pytest

from ddak.core.candidate import preflight_source, scan_staged, tree_manifest
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
    assert result["candidate_sha"] != v2
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


@pytest.mark.parametrize(
    "name", [".env", "private.pem", "private.key", ".ENV", "secret.KEY", "cert.PEM"]
)
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


@pytest.mark.parametrize("name", [".env.template", "config/env.template"])
def test_configured_env_template_is_preserved_and_scanned(repository, tmp_path, name):
    repo, bare, _v1, _v2 = repository
    git(repo.path, "switch", "-c", "prod", "origin/prod")
    template = repo.path / name
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text("APP_ENV=production\n")
    (repo.path / "deploy.yaml").write_text("tiers: {was: {}}\nenv_example: " + name + "\n")
    git(repo.path, "add", "deploy.yaml", name)
    git(repo.path, "commit", "-m", "Configure environment template")
    sha = git(repo.path, "rev-parse", "HEAD")
    git(repo.path, "push", "origin", "prod")
    scans = []
    repo.secret_scan = lambda path: scans.append((path / name).read_text())
    files = file_manifest(repo.path)
    result = repo.prepare_candidate(sha, files, files, None, tmp_path / "candidate", lambda: None)
    candidate = result["candidate_sha"]
    assert name not in tree_manifest(repo, candidate)
    assert git(bare, "show", candidate + ":" + name) == "APP_ENV=production"
    repo.validate_candidate(sha, candidate, files, files, tmp_path / "validate", lambda: None)
    assert scans and all(s == "APP_ENV=production\n" for s in scans)


@pytest.mark.parametrize(
    "name",
    [".env", ".ENV", "secret.KEY", "cert.PEM", ".secrets/env.template", ".env-dir/env.template"],
)
def test_configured_template_cannot_allow_secret_or_excluded_parent(repository, name):
    repo, _bare, _v1, _v2 = repository
    template = repo.path / name
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text("fixture-only\n")
    (repo.path / "deploy.yaml").write_text("tiers: {was: {}}\nenv_example: " + name + "\n")
    git(repo.path, "add", "deploy.yaml", name)
    git(repo.path, "commit", "-m", "Invalid template fixture")
    with pytest.raises(DdakToolError, match="지원하지 않는 파일"):
        tree_manifest(repo, git(repo.path, "rev-parse", "HEAD"))


@pytest.mark.parametrize("changed", ["source", "patch", "deleted", "added"])
def test_configured_template_cannot_diverge_from_approved_build(repository, tmp_path, changed):
    repo, bare, _v1, previous = repository
    git(repo.path, "switch", "-c", "prod", "origin/prod")
    (repo.path / "config").mkdir()
    if changed != "added":
        (repo.path / "config/env.template").write_text("APP_ENV=production\n")
    (repo.path / "deploy.yaml").write_text("tiers: {was: {}}\nenv_example: config/env.template\n")
    git(repo.path, "add", "--all", ".")
    git(repo.path, "commit", "-m", "Add configured template")
    sha = git(repo.path, "rev-parse", "HEAD")
    git(repo.path, "push", "origin", "prod")
    source = file_manifest(repo.path)
    if changed == "deleted":
        (repo.path / "config/env.template").unlink()
    else:
        (repo.path / "config/env.template").write_text("APP_ENV=changed\n")
    build = file_manifest(repo.path)
    if changed in {"source", "added"}:
        source = build
    repo.secret_scan = lambda path: None
    with pytest.raises(DdakToolError, match="prod 템플릿과 승인"):
        repo.prepare_candidate(sha, source, build, None, tmp_path / "candidate", lambda: None)
    assert git(bare, "rev-parse", "ai-prod") == previous

    with pytest.raises(DdakToolError, match="prod 템플릿과 승인"):
        repo.validate_candidate(sha, previous, source, build, tmp_path / "check", lambda: None)


def test_old_ai_config_can_be_invalid_when_approved_prod_is_valid(repository, tmp_path):
    repo, bare, _v1, _v2 = repository
    (repo.path / "deploy.yaml").write_text("tiers: {was: {}}\nobsolete_setting: true\n")
    git(repo.path, "add", "deploy.yaml")
    git(repo.path, "commit", "-m", "Old config format")
    git(repo.path, "push", "origin", "ai-prod")
    git(repo.path, "switch", "-c", "prod", "origin/prod")
    (repo.path / "deploy.yaml").write_text("tiers: {was: {}}\nenv_example: config/env.template\n")
    (repo.path / "config").mkdir()
    (repo.path / "config/env.template").write_text("APP_ENV=production\n")
    git(repo.path, "add", ".")
    git(repo.path, "commit", "-m", "Valid product config")
    git(repo.path, "push", "origin", "prod")
    sha = git(repo.path, "rev-parse", "HEAD")
    files = file_manifest(repo.path)
    repo.secret_scan = lambda path: None
    result = repo.prepare_candidate(sha, files, files, None, tmp_path / "candidate", lambda: None)
    assert (
        git(bare, "show", result["candidate_sha"] + ":deploy.yaml")
        == (repo.path / "deploy.yaml").read_text().strip()
    )


@pytest.mark.parametrize(
    "name", ["Var/x.py", "Instance/x.py", "Node_Modules/x.py", ".Cache/x.py", "data.Sqlite"]
)
def test_case_sensitive_ordinary_source_paths_are_allowed(repository, name):
    repo, _bare, _v1, _v2 = repository
    path = repo.path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ordinary source\n")
    git(repo.path, "add", "--force", name)
    git(repo.path, "commit", "-m", "Ordinary source path")
    assert name in tree_manifest(repo, "HEAD")


@pytest.mark.parametrize(
    "name", [".eNv", ".EnV.local", ".ENV-dir/file.py", "config/.eNv.production"]
)
def test_mixed_case_dot_env_paths_remain_blocked(repository, name):
    repo, _bare, _v1, _v2 = repository
    path = repo.path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fixture only\n")
    git(repo.path, "add", "--force", name)
    git(repo.path, "commit", "-m", "Excluded fixture path")
    with pytest.raises(DdakToolError):
        tree_manifest(repo, "HEAD")


def synthetic_token(seed=b"preflight fixture"):
    return "gh" + "p_" + hashlib.sha256(seed).hexdigest()[:36]


def ignored_prod(repo, *, name="legacy.py", hashed=False, ignore=None):
    git(repo.path, "switch", "-c", "prod", "origin/prod")
    (repo.path / name).write_text("TOKEN = " + repr(synthetic_token()) + "\n")
    # 앱의 설정과 inline allow는 trusted 검사를 끄지 못한다.
    (repo.path / ".gitleaks.toml").write_text('[allowlist]\npaths = [".*"]\n')
    git(repo.path, "add", "--all")
    git(repo.path, "commit", "-m", "Synthetic finding")
    finding_sha = git(repo.path, "rev-parse", "HEAD")
    fingerprint = (finding_sha + ":" if hashed else "") + name + ":github-pat:1"
    (repo.path / ".gitleaksignore").write_text(fingerprint if ignore is None else ignore)
    git(repo.path, "add", ".gitleaksignore")
    git(repo.path, "commit", "-m", "Exact fixture exception")
    git(repo.path, "push", "origin", "prod")
    return git(repo.path, "rev-parse", "HEAD"), fingerprint


@pytest.mark.parametrize("patch", [None, PATCH])
def test_preflight_injected_scanner_exports_pinned_source_and_labels_fixture(repository, patch):
    repo, _bare, v1, _v2 = repository
    seen = []
    repo.secret_scan = lambda path: seen.append(
        ((path / "app.py").read_text(), (path / ".git").exists())
    )
    assert repo.preflight_source(v1, patch=patch) == {
        "source": "fixture",
        "ignored_count": 0,
        "findings": [],
        "git_auth_source": "machine",
    }
    assert seen == [("version = 1\n", False)] + ([("version = 2\n", False)] if patch else [])
    assert {t["operation"] for t in repo.timings} <= {
        "fetch",
        "merge-base",
        "cat-file",
        "ls-tree",
        "check-ref-format",
        "ls-remote",
        "push",
    }


def test_preflight_fetches_missing_sha_in_reused_no_checkout(repository, tmp_path):
    repo, bare, _v1, _v2 = repository
    checkout = shared.AppRepository.connect(tmp_path / "reused", bare.as_uri(), allow_local=True)
    sha = advance_prod(repo)
    with pytest.raises(DdakToolError):
        checkout.git("cat-file", "-e", sha + "^{commit}")
    seen = []
    checkout.secret_scan = lambda path: seen.append((path / "README").read_text())
    assert preflight_source(checkout, sha) == {
        "source": "fixture",
        "ignored_count": 0,
        "findings": [],
    }
    assert seen == ["New feature\n"]
    assert checkout.git("rev-parse", "refs/remotes/origin/prod") == sha
    assert not (checkout.path / "app.py").exists()


def test_preflight_rejects_sha_outside_prod_before_scan(repository):
    repo, _bare, _v1, candidate = repository
    seen = []
    repo.secret_scan = seen.append
    with pytest.raises(DdakToolError):
        preflight_source(repo, candidate)
    assert seen == []


@pytest.mark.parametrize("name", [".env", ".env.example", "deploy.yaml", "config/link.py"])
def test_preflight_rejects_symlinks_before_export_or_scan(repository, name):
    repo, _bare, _v1, _v2 = repository
    git(repo.path, "switch", "-c", "prod", "origin/prod")
    path = repo.path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to("missing-fixture-target")
    git(repo.path, "add", "--force", name)
    git(repo.path, "commit", "-m", "Unsupported link fixture")
    git(repo.path, "push", "origin", "prod")
    scans = []
    repo.secret_scan = scans.append
    with pytest.raises(DdakToolError):
        preflight_source(repo, git(repo.path, "rev-parse", "HEAD"))
    assert scans == []


@pytest.mark.parametrize("name", [".env", ".ENV", "private.key", ".secrets/file.py"])
def test_preflight_rejects_forbidden_files_before_scan(repository, name):
    repo, _bare, _v1, _v2 = repository
    git(repo.path, "switch", "-c", "prod", "origin/prod")
    path = repo.path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic fixture only\n")
    git(repo.path, "add", "--force", name)
    git(repo.path, "commit", "-m", "Unsupported path fixture")
    git(repo.path, "push", "origin", "prod")
    scans = []
    repo.secret_scan = scans.append
    with pytest.raises(DdakToolError, match="지원하지 않는 파일"):
        preflight_source(repo, git(repo.path, "rev-parse", "HEAD"))
    assert scans == []


@pytest.mark.skipif(not shutil.which("gitleaks"), reason="Gitleaks 설치 필요")
@pytest.mark.parametrize("hashed", [False, True])
@pytest.mark.parametrize("name", ["legacy.py", ".env.example"])
def test_exact_prod_exception_survives_unchanged_candidate_and_validation(
    repository, tmp_path, hashed, name
):
    repo, _bare, _v1, _v2 = repository
    sha, _fingerprint = ignored_prod(repo, name=name, hashed=hashed)
    expected = {
        "source": "gitleaks",
        "ignored_count": 1,
        "findings": [{"count": 1, "rule": "github-pat", "path": name}],
    }
    assert preflight_source(repo, sha) == expected
    assert preflight_source(repo, sha, patch=PATCH) == expected
    source = file_manifest(repo.path)
    (repo.path / "app.py").write_text("version = 2\n")
    build = file_manifest(repo.path)
    result = repo.prepare_candidate(sha, source, build, PATCH, tmp_path / "candidate", lambda: None)
    assert result["source_preflight"] == expected
    assert (
        repo.validate_candidate(
            sha, result["candidate_sha"], source, build, tmp_path / "check", lambda: None
        )
        == expected
    )
    assert synthetic_token() not in json.dumps(result)
    assert synthetic_token() not in json.dumps(expected)


@pytest.mark.skipif(not shutil.which("gitleaks"), reason="Gitleaks 설치 필요")
@pytest.mark.parametrize(
    "ignore",
    ["", "legacy.py:github-pat:2", "other.py:github-pat:1", "legacy.py:other-rule:1"],
)
def test_preflight_does_not_match_inexact_fingerprint(repository, ignore):
    repo, _bare, _v1, _v2 = repository
    sha, _fingerprint = ignored_prod(repo, ignore=ignore)
    with pytest.raises(DdakToolError, match="비밀값 검사 실패") as caught:
        preflight_source(repo, sha)
    assert synthetic_token() not in str(caught.value)


@pytest.mark.parametrize(
    "ignore",
    [
        "*",
        "*.py:github-pat:1",
        "legacy.py:*:1",
        "legacy.py:github-pat:*",
        "../legacy.py:github-pat:1",
    ],
)
def test_preflight_rejects_path_or_wildcard_ignore(repository, ignore):
    repo, _bare, _v1, _v2 = repository
    sha, _fingerprint = ignored_prod(repo, ignore=ignore)
    scans = []
    repo.secret_scan = scans.append
    with pytest.raises(DdakToolError, match="정확한 fingerprint"):
        preflight_source(repo, sha)
    assert scans == []


@pytest.mark.skipif(not shutil.which("gitleaks"), reason="Gitleaks 설치 필요")
@pytest.mark.parametrize("change", ["new_secret", "unrelated_line", "new_file", "new_ignore"])
def test_modified_or_added_candidate_content_never_inherits_exception(repository, tmp_path, change):
    repo, bare, _v1, previous = repository
    sha, _fingerprint = ignored_prod(repo)
    source = file_manifest(repo.path)
    if change == "new_secret":
        (repo.path / "legacy.py").write_text("TOKEN = " + repr(synthetic_token(b"new")) + "\n")
    elif change == "unrelated_line":
        with (repo.path / "legacy.py").open("a") as stream:
            stream.write("OTHER = 2\n")
    else:
        (repo.path / "new.py").write_text("TOKEN = " + repr(synthetic_token(b"new")) + "\n")
        if change == "new_ignore":
            with (repo.path / ".gitleaksignore").open("a") as stream:
                stream.write("\nnew.py:github-pat:1\n")
    build = file_manifest(repo.path)
    with pytest.raises(DdakToolError, match="비밀값 검사 실패"):
        repo.prepare_candidate(
            sha, source, build, None, tmp_path / "candidate", lambda: None, repo.path
        )
    assert git(bare, "rev-parse", "ai-prod") == previous
    assert json.loads((tmp_path / "candidate-attempt.json").read_text())["phase"] == "MERGED"

    # 외부에서 주어진 후보 SHA에도 동일 규칙이 적용된다.
    git(repo.path, "add", "--all")
    git(repo.path, "commit", "-m", "Rejected synthetic candidate")
    candidate = git(repo.path, "rev-parse", "HEAD")
    git(bare, "fetch", "--no-tags", str(repo.path), candidate)
    git(bare, "update-ref", "refs/heads/ai-prod", candidate)
    git(repo.path, "update-ref", "refs/remotes/origin/ai-prod", candidate)
    with pytest.raises(DdakToolError, match="비밀값 검사 실패"):
        repo.validate_candidate(sha, candidate, source, build, tmp_path / "check", lambda: None)


@pytest.mark.skipif(not shutil.which("gitleaks"), reason="Gitleaks 설치 필요")
@pytest.mark.parametrize("stale", ["changed_blob", "unrelated_commit"])
def test_commit_fingerprint_cannot_ignore_different_blob_or_history(repository, stale):
    repo, _bare, _v1, unrelated = repository
    _sha, fingerprint = ignored_prod(repo, hashed=True)
    if stale == "changed_blob":
        (repo.path / "legacy.py").write_text("TOKEN = " + repr(synthetic_token(b"new")) + "\n")
    else:
        (repo.path / ".gitleaksignore").write_text(unrelated + ":" + fingerprint.split(":", 1)[1])
    git(repo.path, "add", "--all")
    git(repo.path, "commit", "-m", "Stale exception fixture")
    git(repo.path, "push", "origin", "prod")
    with pytest.raises(DdakToolError, match="비밀값 검사 실패"):
        preflight_source(repo, git(repo.path, "rev-parse", "HEAD"))


@pytest.mark.skipif(not shutil.which("gitleaks"), reason="Gitleaks 설치 필요")
def test_standalone_strict_scan_does_not_use_app_ignore(tmp_path):
    (tmp_path / "legacy.py").write_text("TOKEN = " + repr(synthetic_token()) + "\n")
    (tmp_path / ".gitleaksignore").write_text("legacy.py:github-pat:1\n")
    with pytest.raises(DdakToolError, match="비밀값 검사 실패"):
        scan_staged(tmp_path)


@pytest.mark.skipif(not shutil.which("gitleaks"), reason="Gitleaks 설치 필요")
@pytest.mark.parametrize("existing_exception", [False, True])
def test_preflight_patch_rejects_changed_exception_and_new_finding(repository, existing_exception):
    repo, _bare, _v1, _v2 = repository
    sha, _fingerprint = ignored_prod(repo)
    name = "legacy.py" if existing_exception else "app.py"
    before = (repo.path / name).read_text()
    after = (
        before.rstrip() + "  \n"
        if existing_exception
        else "TOKEN = " + repr(synthetic_token()) + "\n"
    )
    patch = f"--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-{before}+{after}".encode()
    with pytest.raises(DdakToolError, match="비밀값 검사 실패"):
        preflight_source(repo, sha, patch=patch)
    assert (repo.path / name).read_text() == before
    assert git(repo.path, "rev-parse", "HEAD") == sha


@pytest.mark.parametrize("name", ["config/env.template", "missing.py"])
def test_preflight_patch_rejects_template_changes_and_nonexisting_files(repository, name):
    repo, _bare, _v1, _v2 = repository
    git(repo.path, "switch", "-c", "prod", "origin/prod")
    (repo.path / "config").mkdir()
    (repo.path / "config/env.template").write_text("VALUE=old\n")
    (repo.path / "deploy.yaml").write_text("tiers: {was: {}}\nenv_example: config/env.template\n")
    git(repo.path, "add", "--all")
    git(repo.path, "commit", "-m", "Template patch fixture")
    git(repo.path, "push", "origin", "prod")
    sha = git(repo.path, "rev-parse", "HEAD")
    scans = []
    repo.secret_scan = scans.append
    patch = f"--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-VALUE=old\n+VALUE=new\n".encode()
    with pytest.raises(DdakToolError, match=r"템플릿|패치 형식/대상"):
        repo.preflight_source(sha, patch=patch)
    assert len(scans) == 1
    assert (repo.path / "config/env.template").read_text() == "VALUE=old\n"
