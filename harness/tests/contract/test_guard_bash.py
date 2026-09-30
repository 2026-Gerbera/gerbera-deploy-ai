"""Claude Code Bash 가드(.claude/hooks/guard_bash.py) 패턴 테스트."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests.support import REPO_ROOT, load_module

HOOK = REPO_ROOT / ".claude" / "hooks" / "guard_bash.py"
guard = load_module(HOOK, "_guard_bash")

BLOCKED = [
    "git commit --no-verify -m 'x'",
    "git commit -n -m 'feat(core): x'",
    "git push --force origin o1/x",
    "git push -f",
    "git push origin +o1/x",
    "git config user.email codex@openai.com",
    "git -c user.email=x@y commit -m 'a'",
    "git config core.hooksPath /dev/null",
    "GIT_AUTHOR_EMAIL=noreply@anthropic.com git commit -m 'x'",
    "git filter-repo --message-callback x",
    "gh pr merge 12 --squash",
    "terraform apply -auto-approve",
    "terraform -chdir=infra/terraform destroy",
    "aws ecs delete-service --service x",
    "aws ecr batch-delete-image --repository-name x",
    "aws s3 rm s3://bucket/key",
    'bash -c "git push --force"',
    "cat .env",
    "grep KEY .env.local",
    "cp .env /tmp/x",
    "cat ~/.aws/credentials",
    "cat infra/terraform/terraform.tfstate",
    "cat ~/.ssh/id_ed25519",
    "cat compose/local/.secrets/mysql_root_password",
    "cat ~/.claude/.credentials.json",
]
ALLOWED = [
    "git push origin main",
    "git push origin HEAD:main",
    "cd a && git push -u origin main",
    "git status",
    "git commit -m 'fix -n flag handling'",
    "git push -u origin o1/executor-lock",
    "git push origin o1/main-fix",
    "git config --get user.email",
    "terraform -chdir=infra/terraform plan",
    "aws ecs describe-services --cluster x",
    "echo 'do not terraform apply'",
    "make check",
    "cat env.example",
    "cat .env.example",
    "source .venv/bin/activate",
    "ls .envrc",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_blocked(command: str) -> None:
    assert guard.violations(command)


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed(command: str) -> None:
    assert guard.violations(command) == []


def _run(stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True, check=False
    )


def test_exit_code_contract() -> None:
    block = _run(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push -f"}}))
    assert block.returncode == 2
    assert "force push" in block.stderr
    allow = _run(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}}))
    assert allow.returncode == 0
    assert _run("not json").returncode == 0
