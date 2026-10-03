"""시연 반복 스크립트(scripts/demo_cycle.py): 로컬 bare 저장소와 가짜 gh로 확인한다.

실제 GitHub·gh 인증은 쓰지 않는다. bare 저장소가 앱 저장소(prod 브랜치, 태그 v1·v2)를 흉내 낸다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support import load_script

dc = load_script("demo_cycle")

REPO = "example/app"
FAKE_GH = """import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
if args[:2] == ["pr", "list"]:
    path = os.environ["FAKE_GH_PRS"]
    sys.stdout.write(open(path, encoding="utf-8").read() if os.path.exists(path) else "[]")
elif args[:2] == ["pr", "create"]:
    sys.stdout.write("https://github.com/example/app/pull/7\\n")
else:
    sys.exit(2)
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bare = tmp_path / "remote.git"
        self.seed = tmp_path / "seed"
        self.gh_log = tmp_path / "gh.jsonl"
        self.gh_prs = tmp_path / "prs.json"
        self.git_calls: list[list[str]] = []

    def refs(self) -> str:
        return git(self.bare, "for-each-ref", "--format=%(refname) %(objectname)")

    def rev(self, spec: str) -> str:
        return git(self.bare, "rev-parse", spec)

    def demo_branches(self) -> list[str]:
        out = git(self.bare, "for-each-ref", "--format=%(refname:short)", "refs/heads/demo/")
        return out.splitlines()

    def gh_calls(self) -> list[list[str]]:
        if not self.gh_log.exists():
            return []
        return [json.loads(line) for line in self.gh_log.read_text().splitlines()]

    def run(self, *args: str) -> int:
        return dc.main([*args, "--repo", REPO, "--remote", str(self.bare)])

    def push_prod(self, commit: str) -> None:
        git(self.seed, "fetch", "--quiet", "origin")
        git(self.seed, "push", "--quiet", "origin", f"{commit}:refs/heads/prod")

    def set_prs(self, *branches: str) -> None:
        items = [
            {
                "number": index + 10,
                "url": f"https://github.com/example/app/pull/{index + 10}",
                "headRefName": branch,
                "headRefOid": self.rev(f"refs/heads/{branch}"),
            }
            for index, branch in enumerate(branches)
        ]
        self.gh_prs.write_text(json.dumps(items))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    if shutil.which("git") is None:
        pytest.skip("git이 없는 환경(사유: 로컬 bare 저장소 시험은 git이 필요)")
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    home = tmp_path / "home"
    home.mkdir()
    (home / "gitconfig").write_text(
        "[user]\n\tname = Demo Operator\n\temail = operator@example.test\n"
        "[init]\n\tdefaultBranch = main\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    e = Env(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "fake_gh.py").write_text(FAKE_GH)
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{tmp_path / 'fake_gh.py'}' \"$@\"\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(e.gh_log))
    monkeypatch.setenv("FAKE_GH_PRS", str(e.gh_prs))

    git(tmp_path, "init", "--quiet", "--bare", str(e.bare))
    git(tmp_path, "clone", "--quiet", str(e.bare), str(e.seed))
    (e.seed / "web").mkdir()
    (e.seed / "was").mkdir()
    (e.seed / "deploy.yaml").write_text("tiers: [web, was]\n")
    (e.seed / "web" / "index.html").write_text("<h1>board</h1>\n")
    (e.seed / "was" / "list.html").write_text("<ul></ul>\n")
    git(e.seed, "add", ".")
    git(e.seed, "commit", "--quiet", "-m", "v1")
    git(e.seed, "tag", "-a", "v1", "-m", "v1 release")
    (e.seed / "was" / "list.html").write_text("<ul></ul><svg></svg>\n")
    git(e.seed, "commit", "--quiet", "-am", "v2 image box")
    git(e.seed, "tag", "v2")
    # 직전 시연이 끝난 상태: prod = v2 내용.
    git(e.seed, "push", "--quiet", "origin", "HEAD:refs/heads/prod", "HEAD:refs/heads/main")
    git(e.seed, "push", "--quiet", "origin", "refs/tags/v1", "refs/tags/v2")

    original = dc.run

    def recording_run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> str:
        if cmd[0] == "git":
            e.git_calls.append(list(cmd))
        return original(cmd, cwd=cwd, check=check)

    monkeypatch.setattr(dc, "run", recording_run)
    return e


def _pushes(e: Env) -> list[list[str]]:
    return [cmd for cmd in e.git_calls if len(cmd) > 1 and cmd[1] == "push"]


def _assert_no_force(e: Env) -> None:
    for cmd in _pushes(e):
        assert not any(arg.startswith(("-f", "--force", "--mirror", "--tags")) for arg in cmd)
        assert "--delete" not in cmd
        refspec = cmd[-1]
        assert not refspec.startswith("+")
        assert ":refs/heads/demo/" in refspec


def test_reset_v1_commit_uses_v1_tree_with_prod_parent_and_no_force(env: Env, capsys) -> None:
    prod_before = env.rev("refs/heads/prod")
    tags_before = git(env.bare, "for-each-ref", "refs/tags")

    assert env.run("reset-v1") == 0

    [branch] = env.demo_branches()
    assert branch.startswith("demo/reset-v1-")
    head = env.rev(f"refs/heads/{branch}")
    assert env.rev(f"{head}^{{tree}}") == env.rev("v1^{tree}")
    assert env.rev(f"{head}^1") == prod_before
    assert git(env.bare, "rev-list", "--parents", "-n", "1", head).split()[1:] == [prod_before]
    # prod·태그는 그대로다(직접 push·태그 변경 없음).
    assert env.rev("refs/heads/prod") == prod_before
    assert git(env.bare, "for-each-ref", "refs/tags") == tags_before
    assert len(_pushes(env)) == 1
    _assert_no_force(env)

    creates = [c for c in env.gh_calls() if c[:2] == ["pr", "create"]]
    assert len(creates) == 1
    create = creates[0]
    assert create[create.index("--base") + 1] == "prod"
    assert create[create.index("--head") + 1] == branch
    assert create[create.index("--repo") + 1] == REPO
    assert not any(c[:2] == ["pr", "merge"] for c in env.gh_calls())
    assert "https://github.com/example/app/pull/7" in capsys.readouterr().out


def test_dry_run_changes_nothing_remote(env: Env, capsys) -> None:
    refs_before = env.refs()

    assert env.run("reset-v1", "--dry-run") == 0

    out = capsys.readouterr().out
    assert "원격 변경 없음" in out
    assert "push 예정: demo/reset-v1-" in out
    assert env.refs() == refs_before
    assert _pushes(env) == []
    assert all(c[:2] == ["pr", "list"] for c in env.gh_calls())


def test_same_tree_reports_no_change(env: Env, capsys) -> None:
    refs_before = env.refs()

    assert env.run("prepare-v2") == 0

    assert "변경 없음" in capsys.readouterr().out
    assert env.refs() == refs_before
    assert _pushes(env) == []
    assert env.gh_calls() == []


def test_full_cycle_reset_then_prepare_v2(env: Env, capsys) -> None:
    assert env.run("reset-v1") == 0
    [reset_branch] = env.demo_branches()
    # GitHub에서 사람이 merge한 것처럼 prod를 그 커밋으로 진행한다.
    env.push_prod(env.rev(f"refs/heads/{reset_branch}"))
    prod_v1 = env.rev("refs/heads/prod")
    env.gh_prs.write_text("[]")

    assert env.run("reset-v1") == 0
    assert "변경 없음" in capsys.readouterr().out

    assert env.run("prepare-v2") == 0
    v2_branch = next(b for b in env.demo_branches() if b.startswith("demo/v2-"))
    head = env.rev(f"refs/heads/{v2_branch}")
    assert env.rev(f"{head}^{{tree}}") == env.rev("v2^{tree}")
    assert env.rev(f"{head}^1") == prod_v1
    _assert_no_force(env)
    titles = [c[c.index("--title") + 1] for c in env.gh_calls() if c[:2] == ["pr", "create"]]
    assert titles == [dc.ACTIONS["reset-v1"].title, dc.ACTIONS["prepare-v2"].title]


def test_status_shows_prod_tree_and_demo_prs(env: Env, capsys) -> None:
    assert env.run("reset-v1") == 0
    [branch] = env.demo_branches()
    git(env.seed, "push", "--quiet", "origin", "HEAD:refs/heads/feature/other")
    env.set_prs(branch, "feature/other")
    capsys.readouterr()

    assert env.run("status") == 0

    out = capsys.readouterr().out
    assert "prod HEAD" in out and "트리=v2" in out
    assert f"#10 {branch} 트리=v1 현재 prod 기준" in out
    assert "feature/other" not in out


def test_existing_fresh_pr_is_reused_and_stale_pr_blocks(env: Env, capsys) -> None:
    assert env.run("reset-v1") == 0
    [branch] = env.demo_branches()
    env.set_prs(branch)

    assert env.run("reset-v1") == 0
    assert "이미 준비된 PR" in capsys.readouterr().out
    assert env.demo_branches() == [branch]

    # 다른 PR이 prod에 merge되어 prod 내용이 바뀌면 기존 PR은 오래된 것이다.
    (env.seed / "was" / "list.html").write_text("<ul>hotfix</ul>\n")
    git(env.seed, "commit", "--quiet", "-am", "hotfix")
    env.push_prod(git(env.seed, "rev-parse", "HEAD"))

    assert env.run("status") == 0
    assert "오래됨" in capsys.readouterr().out
    assert env.run("reset-v1") == 1
    assert "오래된 reset-v1 PR" in capsys.readouterr().err
    assert env.demo_branches() == [branch]


def test_push_guard_rejects_non_demo_branch(env: Env, tmp_path: Path) -> None:
    with pytest.raises(dc.DemoError):
        dc.push_branch(tmp_path, "0" * 40, "prod")
    with pytest.raises(dc.DemoError):
        dc.push_branch(tmp_path, "0" * 40, "feature/x")
    assert _pushes(env) == []


def test_dirty_clone_is_refused(env: Env, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    original = dc.git

    def dirty_git(clone: Path, *args: str, check: bool = True) -> str:
        if args[:1] == ("status",):
            return " M was/list.html"
        return original(clone, *args, check=check)

    monkeypatch.setattr(dc, "git", dirty_git)
    refs_before = env.refs()

    assert env.run("reset-v1") == 1

    assert "깨끗하지 않다" in capsys.readouterr().err
    assert env.refs() == refs_before


def test_scrub_hides_credentials_and_tokens() -> None:
    token = "gh" + "p_" + "A1b2C3d4" * 5
    text = f"fatal: https://user:{token}@github.com/x.git denied {token}"
    cleaned = dc.scrub(text)
    assert token not in cleaned
    assert "https://***@github.com/x.git" in cleaned


def test_repo_argument_is_validated() -> None:
    with pytest.raises(SystemExit) as exc:
        dc.main(["status", "--repo", "bad repo; rm"])
    assert exc.value.code == 2
