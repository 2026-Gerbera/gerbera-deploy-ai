"""앱 설정에서 조립한 checkout과 실제 push URL을 로컬 bare로 검증한다."""

from dataclasses import replace

import pytest

from ddak import app
from ddak.core.app_repository import AppRepository
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError
from tests.unit.core.test_app_repository import git


@pytest.fixture
def connected(tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", bare.as_uri(), str(seed))
    (seed / "app.py").write_text("version=1\n")
    git(seed, "add", "app.py")
    git(seed, "commit", "-m", "Seed")
    git(seed, "push", "origin", "HEAD:prod", "HEAD:ai-prod", "HEAD:main")
    ctx = RunContext("connection-run", project="demo", repo_url=bare.as_uri(), ref="prod")
    factory = app._repository_factory(tmp_path / "repositories", allow_local=True)
    return factory(ctx), ctx, factory, bare


def test_factory_reconnects_same_repository_without_manual_checkout(connected):
    repo, ctx, factory, bare = connected
    assert factory(ctx).path == repo.path
    assert repo.expected_url == ctx.repo_url
    assert repo.git("remote", "get-url", "--push", "origin") == bare.as_uri()


@pytest.mark.parametrize("change", ["origin", "pushurl", "multiple", "rewrite"])
def test_origin_changes_are_rejected_before_push(connected, tmp_path, change):
    repo, ctx, factory, bare = connected
    other = tmp_path / "other.git"
    git(tmp_path, "init", "--bare", str(other))
    if change == "origin":
        git(repo.path, "remote", "set-url", "origin", other.as_uri())
    elif change == "pushurl":
        git(repo.path, "remote", "set-url", "--push", "origin", other.as_uri())
    elif change == "multiple":
        git(repo.path, "remote", "set-url", "--add", "--push", "origin", ctx.repo_url)
        git(repo.path, "remote", "set-url", "--add", "--push", "origin", other.as_uri())
    else:
        git(repo.path, "config", "url." + other.as_uri() + ".pushInsteadOf", ctx.repo_url)
    with pytest.raises(DdakToolError, match="origin"):
        repo.git("push", "origin", "refs/remotes/origin/prod:refs/heads/unapproved")
    with pytest.raises(DdakToolError):
        factory(ctx)
    assert not git(other, "for-each-ref")
    assert not git(bare, "for-each-ref", "refs/heads/unapproved")


def test_other_approved_url_and_fake_remote_fail_closed(connected):
    repo, ctx, factory, _ = connected
    with pytest.raises(DdakToolError):
        repo.require_origin("https://example.test/other.git")
    with pytest.raises(DdakToolError, match="FAKE"):
        factory(replace(ctx, repo_url="https://example.test/other.git"))


def test_connect_refuses_nonempty_directory_without_discarding_files(tmp_path):
    path = tmp_path / "checkout"
    path.mkdir()
    (path / "keep.txt").write_text("keep")
    with pytest.raises(DdakToolError):
        AppRepository.connect(path, (tmp_path / "bare.git").as_uri(), allow_local=True)
    assert (path / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize("annotated", [True, False])
def test_tag_resolves_commit_not_tag_object_or_same_named_branch(connected, annotated):
    repo, _ctx, _factory, bare = connected
    seed = bare.parent / "seed"
    commit = git(seed, "rev-parse", "HEAD")
    args = ("-a", "v1", "-m", "Release") if annotated else ("v1",)
    git(seed, "tag", *args)
    git(seed, "push", "origin", "refs/tags/v1")
    (seed / "app.py").write_text("version=2\n")
    git(seed, "add", "app.py")
    git(seed, "commit", "-m", "Next")
    git(seed, "push", "origin", "HEAD:refs/heads/v1")
    assert repo.resolve_tag("refs/tags/v1") == commit
    if annotated:
        assert git(bare, "rev-parse", "refs/tags/v1") != commit
    with pytest.raises(DdakToolError, match="찾을 수 없다"):
        repo.resolve_tag("refs/tags/v-missing")
