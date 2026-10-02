"""외부 네트워크 없이 로컬 bare 저장소의 실제 ref 이동을 검증한다."""

import subprocess
from pathlib import Path

import pytest

from ddak.core.app_repository import AppRepository


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Local Operator", "-c", "user.email=operator@example.test", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(bare))
    work = tmp_path / "app"
    git(tmp_path, "clone", str(bare), str(work))
    git(work, "switch", "-c", "main")
    (work / "app.py").write_text("version = 1\n")
    git(work, "add", "app.py")
    git(work, "commit", "-m", "First version")
    v1 = git(work, "rev-parse", "HEAD")
    git(
        work,
        "push",
        "origin",
        "main",
        "HEAD:prod",
        "HEAD:ai-prod",
        "HEAD:refs/tags/deployed/onprem",
        "HEAD:refs/tags/deployed/cloud",
    )
    git(work, "switch", "-c", "ai-prod")
    (work / "app.py").write_text("version = 2\n")
    git(work, "add", "app.py")
    git(work, "commit", "-m", "Second version")
    v2 = git(work, "rev-parse", "HEAD")
    git(work, "push", "origin", "ai-prod")
    return AppRepository(work, allow_local=True), bare, v1, v2


@pytest.mark.parametrize(
    "selected,succeeded,main_moves",
    [
        ({"local", "cloud"}, {"local"}, False),
        ({"local", "cloud"}, {"cloud"}, False),
        ({"local", "cloud"}, {"local", "cloud"}, True),
        ({"local"}, {"local"}, True),
        ({"cloud"}, {"cloud"}, True),
        ({"local", "cloud"}, set(), False),
    ],
)
def test_publish_only_successful_environment_refs(repository, selected, succeeded, main_moves):
    repo, bare, v1, v2 = repository
    result = repo.publish(v2, selected, succeeded)
    assert result["status"] == ("SUCCEEDED" if succeeded else "SKIPPED")
    assert result["main_updated"] is main_moves
    assert git(bare, "rev-parse", "main") == (v2 if main_moves else v1)
    for target, tag in (("local", "onprem"), ("cloud", "cloud")):
        assert git(bare, "rev-parse", "refs/tags/deployed/" + tag) == (
            v2 if target in succeeded else v1
        )


def test_diverged_main_is_preserved_but_successful_tags_recorded(repository):
    repo, bare, _v1, v2 = repository
    git(repo.path, "switch", "main")
    (repo.path / "other.py").write_text("version = 3\n")
    git(repo.path, "add", "other.py")
    git(repo.path, "commit", "-m", "Concurrent main update")
    advanced = git(repo.path, "rev-parse", "HEAD")
    git(repo.path, "push", "origin", "main")
    result = repo.publish(v2, {"local"}, {"local"})
    assert result["status"] == "PARTIAL" and not result["main_updated"]
    assert git(bare, "rev-parse", "main") == advanced
    assert git(bare, "rev-parse", "refs/tags/deployed/onprem") == v2


def test_tag_lease_prevents_concurrent_overwrite(repository, monkeypatch):
    repo, bare, v1, v2 = repository
    run = repo.git

    def race(*args, **kwargs):
        if args[0] == "push" and "--atomic" in args:
            git(bare, "update-ref", "refs/tags/deployed/onprem", v2, v1)
        return run(*args, **kwargs)

    monkeypatch.setattr(repo, "git", race)
    result = repo.publish(v1, {"local"}, {"local"})
    assert result["status"] == "FAILED"
    assert git(bare, "rev-parse", "refs/tags/deployed/onprem") == v2
    assert git(bare, "rev-parse", "main") == v1
