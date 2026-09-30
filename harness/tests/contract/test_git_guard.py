"""scripts/git_guard.py 테스트: AI 줄 제거와 AI 작성자 차단, 자유로운 Git 사용.

마지막 두 테스트는 임시 git 저장소에서 실제 훅(.githooks)으로 커밋·검사한다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.support import REPO_ROOT, load_script

gg = load_script("git_guard")

ALICE = "111+alice@users.noreply.github.com"
BOB = "222+bob@users.noreply.github.com"
MESSAGE = """feat(executor): 잠금 토큰 전달

본문.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-authored-by: Bob Lee <222+bob@users.noreply.github.com>
Co-Authored-By: Claude Opus <noreply@anthropic.com>
Co-authored-by: Codex <noreply@openai.com>
Claude-Session: https://claude.ai/code/session_x
"""


def test_strip_keeps_human_co_author_and_removes_ai_lines() -> None:
    cleaned, removed = gg.strip_ai_lines(MESSAGE)
    assert "Co-authored-by: Bob Lee <222+bob@users.noreply.github.com>" in cleaned
    assert "anthropic" not in cleaned
    assert "openai" not in cleaned
    assert "Claude-Session" not in cleaned
    assert "Generated with" not in cleaned
    assert len(removed) == 4


def test_strip_is_noop_for_clean_message() -> None:
    msg = "fix(core): 오타 수정\n"
    assert gg.strip_ai_lines(msg) == (msg, [])


def test_strip_never_removes_subject_line() -> None:
    # 제목이 지워지면 본문 첫 줄이 제목이 된다. 제목은 남기고 check_subject_ai가 거부한다.
    msg = "docs(harness): generated with claude 문구 차단 설명\n\n본문\n"
    cleaned, removed = gg.strip_ai_lines(msg)
    assert removed == []
    assert cleaned.startswith("docs(harness): generated with claude")
    assert gg.check_subject_ai(gg.subject_of(cleaned))


def test_session_url_line_is_ai_line() -> None:
    # attribution.sessionUrl 기본값은 PR 본문에 claude.ai 세션 링크를 붙인다.
    assert gg.is_ai_line("https://claude.ai/code/session_01abc")
    assert gg.check_pr_text("feat(core): x", "요약\n\nhttps://claude.ai/code/session_01abc")


@pytest.mark.parametrize(
    "email",
    ["198982749+Copilot@users.noreply.github.com", "cursoragent@cursor.com"],
)
def test_agent_default_identities_are_ai(email: str) -> None:
    assert gg.check_identity("author", "bot", email)


def _commit(author: str, message: str, committer: str = ALICE) -> object:
    return gg.CommitInfo("abc", "A", author, "C", committer, message)


def test_commit_by_human_author_passes() -> None:
    msg = "feat(core): x\n\nCo-authored-by: Bob <222+bob@users.noreply.github.com>\n"
    assert gg.check_commit(_commit(ALICE, msg)) == []


@pytest.mark.parametrize(
    ("author", "message"),
    [
        ("noreply@anthropic.com", "feat(core): x\n"),
        ("codex@openai.com", "feat(core): x\n"),
        (ALICE, "feat(core): x\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"),
        (ALICE, "feat(core): x\n\nClaude-Session: https://claude.ai/code/s\n"),
    ],
)
def test_commit_violations(author: str, message: str) -> None:
    assert gg.check_commit(_commit(author, message))


def test_github_web_committer_is_allowed() -> None:
    assert gg.check_commit(_commit(ALICE, "feat(core): x\n", "noreply@github.com")) == []


def test_human_author_committer_and_coauthor_need_no_registration() -> None:
    message = "자유로운 제목\n\nCo-authored-by: Eve <eve@example.com>\n"
    assert gg.check_commit(_commit("dev@example.com", message, "builder@example.org")) == []


@pytest.mark.parametrize("name", ["Devin Lee", "Claude Martin"])
def test_human_coauthor_name_is_not_an_ai_identity(name: str) -> None:
    message = f"update\n\nCo-authored-by: {name} <human@example.org>\n"
    assert gg.strip_ai_lines(message) == (message, [])
    assert gg.check_commit(_commit(ALICE, message)) == []


def test_ai_committer_and_email_only_ai_coauthor_are_rejected() -> None:
    assert "AI/봇 신원" in " ".join(gg.check_commit(_commit(ALICE, "update\n", "codex@openai.com")))
    message = "update\n\nCo-authored-by: Assistant <cursoragent@cursor.com>\n"
    assert "AI attribution" in " ".join(gg.check_commit(_commit(ALICE, message)))


def test_pr_text() -> None:
    assert gg.check_pr_text("feat(core): x", "설명") == []
    body = "요약\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"
    assert gg.check_pr_text("feat(core): x", body)
    assert gg.check_pr_text("아무 제목", "") == []


# ---------------------------------------------------------------- 실제 훅 (임시 저장소)
# .py를 스테이징하면 pre-commit이 .venv의 ruff를 요구하므로 txt 파일만 커밋한다.
def _git_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(HOME=str(home), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(home / "gitconfig"))
    return env


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=False)


@pytest.fixture
def hook_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    if shutil.which("git") is None or shutil.which("bash") is None:
        pytest.skip("git/bash가 없는 환경(사유: 훅 통합 테스트는 git과 bash가 필요)")
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".github").mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / "git_guard.py", repo / "scripts" / "git_guard.py")
    shutil.copytree(REPO_ROOT / ".githooks", repo / ".githooks")
    env = _git_env(tmp_path)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "core.hooksPath", ".githooks"],
        ["git", "config", "user.useConfigOnly", "true"],
        ["git", "config", "user.name", "Alice"],
        ["git", "config", "user.email", ALICE],
    ):
        assert _run(args, repo, env).returncode == 0
    return repo, env


def test_commit_msg_hook_strips_claude_trailer(hook_repo: tuple[Path, dict[str, str]]) -> None:
    repo, env = hook_repo
    (repo / "a.txt").write_text("a\n")
    _run(["git", "add", "a.txt"], repo, env)
    msg = "chore(harness): 초기 파일\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
    result = _run(["git", "commit", "-q", "-m", msg], repo, env)
    assert result.returncode == 0, result.stderr
    body = _run(["git", "log", "-1", "--format=%B"], repo, env).stdout
    assert "anthropic" not in body
    assert body.startswith("chore(harness): 초기 파일")


def test_commit_msg_hook_allows_free_subject(hook_repo: tuple[Path, dict[str, str]]) -> None:
    repo, env = hook_repo
    (repo / "b.txt").write_text("b\n")
    _run(["git", "add", "b.txt"], repo, env)
    result = _run(["git", "commit", "-q", "-m", "update"], repo, env)
    assert result.returncode == 0, result.stderr


def test_commit_msg_hook_rejects_ai_phrase_in_subject(
    hook_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = hook_repo
    (repo / "s.txt").write_text("s\n")
    _run(["git", "add", "s.txt"], repo, env)
    subject = "chore(harness): Generated with Claude Code"
    result = _run(["git", "commit", "-q", "-m", subject], repo, env)
    assert result.returncode != 0
    assert "커밋 제목에 AI attribution" in result.stderr


def _bare_remote(repo: Path, env: dict[str, str]) -> Path:
    remote = repo.parent / "origin.git"
    assert _run(["git", "init", "-q", "--bare", str(remote)], repo, env).returncode == 0
    assert _run(["git", "remote", "add", "origin", str(remote)], repo, env).returncode == 0
    return remote


def test_pre_push_allows_main_creation_and_updates(hook_repo: tuple[Path, dict[str, str]]) -> None:
    repo, env = hook_repo
    _bare_remote(repo, env)
    (repo / "m.txt").write_text("m\n")
    _run(["git", "add", "m.txt"], repo, env)
    assert _run(["git", "commit", "-q", "-m", "chore(harness): 첫 커밋"], repo, env).returncode == 0
    first = _run(["git", "push", "-q", "origin", "main"], repo, env)
    assert first.returncode == 0, first.stderr
    (repo / "n.txt").write_text("n\n")
    _run(["git", "add", "n.txt"], repo, env)
    assert _run(["git", "commit", "-q", "-m", "chore(harness): 두 번째"], repo, env).returncode == 0
    second = _run(["git", "push", "-q", "origin", "main"], repo, env)
    assert second.returncode == 0, second.stderr


def test_pre_push_rejects_bypassed_commit_on_new_branch(
    hook_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = hook_repo
    _bare_remote(repo, env)
    (repo / "p.txt").write_text("p\n")
    _run(["git", "add", "p.txt"], repo, env)
    assert _run(["git", "commit", "-q", "-m", "chore(harness): 첫 커밋"], repo, env).returncode == 0
    assert _run(["git", "push", "-q", "origin", "main"], repo, env).returncode == 0
    _run(["git", "switch", "-q", "-c", "o1/x"], repo, env)
    (repo / "q.txt").write_text("q\n")
    _run(["git", "add", "q.txt"], repo, env)
    msg = "fix(core): x\n\nCo-authored-by: Codex <codex@openai.com>\n"
    assert _run(["git", "commit", "-q", "--no-verify", "-m", msg], repo, env).returncode == 0
    result = _run(["git", "push", "-q", "origin", "o1/x"], repo, env)
    assert result.returncode != 0
    assert "AI attribution" in result.stderr


def test_range_catches_ai_author_even_with_no_verify(
    hook_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = hook_repo
    (repo / "c.txt").write_text("c\n")
    _run(["git", "add", "c.txt"], repo, env)
    bot_env = {**env, "GIT_AUTHOR_NAME": "Claude", "GIT_AUTHOR_EMAIL": "noreply@anthropic.com"}
    bypass = _run(["git", "commit", "-q", "--no-verify", "-m", "fix(core): x"], repo, bot_env)
    assert bypass.returncode == 0
    result = _run(["python3", "scripts/git_guard.py", "range", "", "HEAD"], repo, env)
    assert result.returncode == 1
    assert "AI/봇 신원" in result.stderr


def test_hooks_work_from_nested_harness_and_ignore_reference_code(
    hook_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = hook_repo
    project = repo / "harness"
    project.mkdir()
    shutil.move(str(repo / "scripts"), project / "scripts")
    shutil.move(str(repo / ".githooks"), project / ".githooks")
    assert _run(["git", "config", "core.hooksPath", "harness/.githooks"], repo, env).returncode == 0
    bindir = project / ".venv/bin"
    bindir.mkdir(parents=True)
    (bindir / "ruff").symlink_to(REPO_ROOT.parent / ".venv/bin/ruff")
    (project / "example.py").write_text("x = 1\n")
    (repo / "reference.py").write_text("not python !!!\n")
    sample = project / "apps/sample-app/example.py"
    sample.parent.mkdir(parents=True)
    sample.write_text("not python !!!\n")
    assert (
        _run(
            ["git", "add", "harness/example.py", "reference.py", "harness/apps"], repo, env
        ).returncode
        == 0
    )
    result = _run(["git", "commit", "-q", "-m", "nested harness"], repo, env)
    assert result.returncode == 0, result.stderr
    # 앱을 루트 src/로 옮겨도 같은 pre-commit 검사를 거친다.
    app = repo / "src/example.py"
    app.parent.mkdir()
    app.write_text("not python !!!\n")
    assert _run(["git", "add", "src/example.py"], repo, env).returncode == 0
    rejected = _run(["git", "commit", "-q", "-m", "invalid source"], repo, env)
    assert rejected.returncode != 0
