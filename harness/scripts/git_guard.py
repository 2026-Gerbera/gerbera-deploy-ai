#!/usr/bin/env python3
"""AI 작성자 표시 차단. 로컬 훅과 CI가 같이 쓰는 단일 출처.

표준 라이브러리만 쓴다(uv 설치 전, CI의 시스템 python3에서도 돈다).

하위 명령:
  commit-msg <file>   AI attribution 줄을 자동 제거한다(사람 co-author는 보존).
  pre-commit          스테이징된 .py에 ruff format --check / ruff check, gitleaks(설치 시).
  pre-push            stdin의 ref 목록을 읽어 push 범위의 AI 작성자 표시를 검사한다.
  range <base> <head> CI용. base..head 모든 커밋의 author, committer, 메시지, co-author 검사.
                      base가 비었거나 0으로만 채워져 있으면 head까지 전체 이력을 검사한다.
  pr-text             환경변수 PR_TITLE / PR_BODY의 AI 문구를 검사한다.

종료 코드: 0 통과, 1 위반, 2 사용법 오류.
팀원 이메일 등록, 커밋 제목 형식, 브랜치명, PR 승인은 강제하지 않는다.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# 패턴 (단일 출처, 대소문자 무시). docs/harness/03 문서와 같은 값을 유지한다.
# ---------------------------------------------------------------------------
SESSION = re.compile(r"^\s*claude-session:", re.IGNORECASE)
# attribution.sessionUrl 기본값: cloud·Remote Control 세션의 PR 본문에 붙는 claude.ai 세션 링크.
SESSION_URL = re.compile(r"claude\.ai/code/session", re.IGNORECASE)
GENERATED = re.compile(
    r"(generated|created) with .*(claude|codex|chatgpt|copilot)|🤖 generated with",
    re.IGNORECASE,
)
AI_EMAIL = re.compile(
    r"(@anthropic\.com|@openai\.com|^codex@|codex@example\.com|\[bot\]@"
    r"|users\.noreply\.github\.com.*\[bot\]"
    # Copilot coding agent, Cursor agent 기본 author.
    r"|\+copilot@users\.noreply\.github\.com|@cursor\.com)",
    re.IGNORECASE,
)
CO_AUTHOR = re.compile(
    r"^\s*co-authored-by:\s*(?P<name>.*?)\s*<(?P<email>[^<>\s]+)>\s*$", re.IGNORECASE
)

ZERO_SHA_RE = re.compile(r"^0+$")
SCISSORS = "# ------------------------ >8 ------------------------"

RUFF_EXCLUDED = ("apps/sample-app/", "fixtures/patch_cases/")


class UsageError(Exception):
    """잘못된 호출. 종료 코드 2."""


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    message: str


def is_ai_line(line: str) -> bool:
    coauthor = CO_AUTHOR.match(line)
    return bool(
        (coauthor and AI_EMAIL.search(coauthor["email"]))
        or SESSION.search(line)
        or SESSION_URL.search(line)
        or GENERATED.search(line)
    )


def strip_ai_lines(message: str) -> tuple[str, list[str]]:
    """AI attribution 줄만 지운다. scissors 아래(커밋 -v의 diff)는 건드리지 않는다.

    제목 줄(주석이 아닌 첫 비어 있지 않은 줄)은 지우지 않는다. 제목이 지워지면 본문 첫 줄이
    제목이 되어 버리므로, 제목에 걸린 경우는 check_subject_ai가 거부한다.
    """
    head, sep, tail = message.partition(SCISSORS)
    kept: list[str] = []
    removed: list[str] = []
    seen_subject = False
    for line in head.splitlines():
        is_comment = line.startswith("#")
        if not is_comment and not seen_subject and line.strip():
            seen_subject = True
            kept.append(line)
        elif not is_comment and is_ai_line(line):
            removed.append(line)
        else:
            kept.append(line)
    if not removed:
        return message, []
    while kept and not kept[-1].strip():
        kept.pop()
    new_head = "\n".join(kept) + "\n"
    return new_head + (sep + tail if sep else ""), removed


def subject_of(message: str) -> str:
    head = message.partition(SCISSORS)[0]
    for line in head.splitlines():
        if line.startswith("#"):
            continue
        if line.strip():
            return line.rstrip()
    return ""


def find_ai_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if is_ai_line(line)]


def check_subject_ai(subject: str) -> list[str]:
    """제목 줄의 AI 문구는 자동으로 지우지 않고 거부한다(제목을 직접 고치게)."""
    if subject and is_ai_line(subject):
        return [f"커밋 제목에 AI attribution 문구가 있다: {subject!r}. 제목을 고쳐 다시 커밋한다."]
    return []


def check_identity(role: str, name: str, email: str) -> list[str]:
    errors: list[str] = []
    lowered = email.strip().lower()
    if AI_EMAIL.search(lowered):
        errors.append(f"{role}가 AI/봇 신원이다: {name} <{email}>")
    return errors


def check_commit(info: CommitInfo) -> list[str]:
    errors = check_identity("author", info.author_name, info.author_email)
    errors += check_identity("committer", info.committer_name, info.committer_email)
    for line in find_ai_lines(info.message):
        errors.append(f"AI attribution 문구: {line.strip()!r}")
    return errors


def check_pr_text(title: str, body: str) -> list[str]:
    errors: list[str] = []
    for label, text in (("PR 제목", title), ("PR 본문", body)):
        for line in find_ai_lines(text):
            errors.append(f"{label}에 AI attribution 문구: {line.strip()!r}")
    return errors


# ---------------------------------------------------------------------------
# git 호출
# ---------------------------------------------------------------------------
def _git(args: Sequence[str], *, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {proc.stderr.strip()}")
    return proc.stdout


def repo_root() -> Path:
    return Path(_git(["rev-parse", "--show-toplevel"]).strip())


def is_zero(sha: str) -> bool:
    return not sha or bool(ZERO_SHA_RE.match(sha))


def rev_list(args: Sequence[str], cwd: Path) -> list[str]:
    return [s for s in _git(["rev-list", *args], cwd=cwd).split() if s]


def commit_info(sha: str, cwd: Path) -> CommitInfo:
    out = _git(["show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B", sha], cwd=cwd)
    an, ae, cn, ce, body = out.split("\x00", 4)
    return CommitInfo(sha, an, ae, cn, ce, body)


def _ident_email(var: str, cwd: Path) -> tuple[str, str] | None:
    try:
        ident = _git(["var", var], cwd=cwd).strip()
    except RuntimeError:
        return None
    match = re.match(r"^(?P<name>.*?)\s*<(?P<email>[^>]*)>", ident)
    return (match["name"], match["email"]) if match else None


def _report(errors: Sequence[str], header: str) -> int:
    if not errors:
        return 0
    print(f"git_guard: {header}", file=sys.stderr)
    for err in errors:
        print(f"  - {err}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# 하위 명령
# ---------------------------------------------------------------------------
def cmd_commit_msg(path: Path) -> int:
    root = repo_root()
    original = path.read_text(encoding="utf-8")
    cleaned, removed = strip_ai_lines(original)
    if removed:
        path.write_text(cleaned, encoding="utf-8")
        print("git_guard: AI attribution 줄을 제거했다(사람 co-author는 유지):", file=sys.stderr)
        for line in removed:
            print(f"  - {line.strip()}", file=sys.stderr)
    subject = subject_of(cleaned)
    errors = check_subject_ai(subject)
    for var, role in (("GIT_AUTHOR_IDENT", "author"), ("GIT_COMMITTER_IDENT", "committer")):
        ident = _ident_email(var, root)
        if ident and AI_EMAIL.search(ident[1]):
            errors.append(f"{role}가 AI/봇 신원이다: {ident[0]} <{ident[1]}>")
    return _report(errors, "커밋을 거부한다")


def _staged_python_files(root: Path) -> list[str]:
    out = _git(
        ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z", "--", "*.py"], cwd=root
    )
    project = Path(__file__).resolve().parents[1]
    prefix = "" if project == root else project.relative_to(root).as_posix() + "/"
    files = [f for f in out.split("\x00") if f and f.startswith(prefix)]
    return [f for f in files if not f.removeprefix(prefix).startswith(RUFF_EXCLUDED)]


def _ruff_command(root: Path) -> list[str] | None:
    # uv.lock에 고정된 ruff(.venv)만 쓴다. `uv run`으로 대체하면 커밋 중에 동기화가 일어난다.
    for candidate in (root / ".venv" / "bin" / "ruff", root / ".venv" / "Scripts" / "ruff.exe"):
        if candidate.is_file():
            return [str(candidate)]
    return None


def cmd_pre_commit() -> int:
    root = repo_root()
    failed = False
    files = _staged_python_files(root)
    if files:
        ruff = _ruff_command(Path(__file__).resolve().parents[1])
        if ruff is None:
            print("git_guard: .venv에 ruff가 없다. `make setup` 후 다시 커밋한다.", file=sys.stderr)
            return 1
        # 참고: 작업 트리 파일을 검사한다(부분 스테이징은 구분하지 않는다).
        for args in (["format", "--check", "--force-exclude"], ["check", "--force-exclude"]):
            proc = subprocess.run([*ruff, *args, *files], cwd=root, check=False)
            failed = failed or proc.returncode != 0
        if failed:
            print("git_guard: ruff 실패. `make fmt` 후 다시 스테이징한다.", file=sys.stderr)
    if shutil.which("gitleaks"):
        proc = subprocess.run(
            ["gitleaks", "git", "--pre-commit", "--staged", "--redact", "--no-banner"],
            cwd=root,
            check=False,
        )
        if proc.returncode != 0:
            print("git_guard: gitleaks가 비밀값 의심 항목을 찾았다.", file=sys.stderr)
            failed = True
    else:
        msg = "git_guard: 경고: gitleaks 미설치. 비밀값은 CI secrets 잡이 검사한다."
        print(msg, file=sys.stderr)
    return 1 if failed else 0


def _push_range(local_sha: str, remote_sha: str, root: Path) -> list[str]:
    """이번 push로 원격에 처음 올라가는 커밋. 이미 어느 원격 ref에든 있는 커밋(예: 브랜치로
    merge해 들인 main의 squash 커밋)은 다시 검사하지 않는다."""
    base = [local_sha, "--not", "--remotes"]
    if not is_zero(remote_sha):
        # 원격 sha가 로컬에 없으면(미fetch) --remotes만으로 범위를 잡는다.
        with contextlib.suppress(RuntimeError):
            return rev_list([*base, remote_sha], root)
    return rev_list(base, root)


def cmd_pre_push(stdin_lines: Iterable[str]) -> int:
    root = repo_root()
    errors: list[str] = []
    for raw in stdin_lines:
        parts = raw.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts
        if is_zero(local_sha):
            continue  # 원격 ref 삭제
        for sha in _push_range(local_sha, remote_sha, root):
            info = commit_info(sha, root)
            errors += [f"{sha[:10]}: {e}" for e in check_commit(info)]
    return _report(errors, "push를 거부한다")


def cmd_range(base: str, head: str) -> int:
    root = repo_root()
    if not head:
        raise UsageError("head가 비어 있다")
    shas = rev_list([head] if is_zero(base) else [f"{base}..{head}"], root)
    errors: list[str] = []
    for sha in shas:
        errors += [f"{sha[:10]}: {e}" for e in check_commit(commit_info(sha, root))]
    code = _report(errors, f"{len(shas)}개 커밋 중 위반이 있다")
    if code == 0:
        print(f"git_guard: {len(shas)}개 커밋 검사 통과")
    return code


def cmd_pr_text() -> int:
    title = os.environ.get("PR_TITLE", "")
    body = os.environ.get("PR_BODY", "")
    if not title:
        raise UsageError("PR_TITLE 환경변수가 비어 있다")
    code = _report(check_pr_text(title, body), "PR 제목·본문 검사 실패")
    if code == 0:
        print("git_guard: PR 제목·본문 검사 통과")
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="git_guard", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_msg = sub.add_parser("commit-msg")
    p_msg.add_argument("file", type=Path)
    sub.add_parser("pre-commit")
    p_push = sub.add_parser("pre-push")
    p_push.add_argument("remote", nargs="?")
    p_push.add_argument("url", nargs="?")
    p_range = sub.add_parser("range")
    p_range.add_argument("base")
    p_range.add_argument("head")
    sub.add_parser("pr-text")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    try:
        if args.cmd == "commit-msg":
            return cmd_commit_msg(args.file)
        if args.cmd == "pre-commit":
            return cmd_pre_commit()
        if args.cmd == "pre-push":
            return cmd_pre_push(sys.stdin.read().splitlines())
        if args.cmd == "range":
            return cmd_range(args.base, args.head)
        return cmd_pr_text()
    except UsageError as exc:
        print(f"git_guard: 사용법 오류: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"git_guard: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
