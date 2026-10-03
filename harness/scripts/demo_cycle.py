"""시연 반복용 앱 저장소 PR 준비: prod를 v1 트리로 되돌리는 PR, 시연용 v2 PR, 상태 확인.

PR merge는 시연 장면이라 사람이 GitHub에서 한다. 이 스크립트는 merge하지 않는다.

- reset-v1: 임시 clone에서 prod를 부모로, 태그 v1의 트리를 그대로 쓰는 커밋을 만든다
  (git commit-tree). demo/reset-v1-<시각> 새 브랜치를 push하고 prod 대상 PR을 연다.
- prepare-v2: 같은 방식으로 태그 v2의 트리 커밋을 만들어 demo/v2-<시각> PR을 연다.
- status: prod HEAD 트리가 v1·v2 중 무엇과 같은지, 열린 demo/* PR이 지금 merge해도 되는지 본다.

안전 조건:
- force push·prod 직접 push·태그 변경을 하지 않는다. demo/ 새 브랜치만 일반 push한다.
- 사용자 git 신원·gh 인증을 그대로 쓴다. git config를 바꾸지 않는다.
- 토큰·자격증명을 출력하지 않는다. 오류 출력은 URL 자격증명과 토큰 모양을 가린다.
- --dry-run은 clone·조회·로컬 커밋 계산만 하고 push·PR 생성을 하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPO = "2026-Gerbera/gerbera-application"
BASE_BRANCH = "prod"
DEMO_PREFIX = "demo/"
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@"), r"\1***@"),
    (re.compile(r"\b(gh[pousr]_|github_pat_)[A-Za-z0-9_]{8,}"), r"\1***"),
)


@dataclass(frozen=True)
class Action:
    tag: str
    branch_prefix: str
    title: str
    purpose: str


ACTIONS = {
    "reset-v1": Action(
        tag="v1",
        branch_prefix="demo/reset-v1-",
        title="demo: prod를 v1 상태로 되돌림(시연 반복 준비)",
        purpose="시연 반복을 위해 prod 내용을 v1으로 되돌린다. merge하면 제품이 v1을 배포한다.",
    ),
    "prepare-v2": Action(
        tag="v2",
        branch_prefix="demo/v2-",
        title="v2: 목록 페이지 이미지·박스 추가",
        purpose="시연용 v2 변경이다. merge하면 제품이 변경을 감지해 WAS만 다시 빌드·배포한다.",
    ),
}


class DemoError(Exception):
    """사용자에게 보여 줄 실패. 메시지는 이미 가려진 문자열이다."""


@dataclass(frozen=True)
class Snapshot:
    prod_sha: str
    prod_tree: str
    tag_commits: dict[str, str]
    tag_trees: dict[str, str]


@dataclass(frozen=True)
class DemoPr:
    number: int
    url: str
    branch: str
    tree: str | None
    parent_tree: str | None

    def kind(self) -> str | None:
        for name, action in ACTIONS.items():
            if self.branch.startswith(action.branch_prefix):
                return name
        return None


def scrub(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        detail = scrub((proc.stderr or proc.stdout).strip())[-800:]
        raise DemoError(f"{' '.join(cmd[:2])} 실패(exit {proc.returncode}): {detail}")
    return proc.stdout.strip()


def git(clone: Path, *args: str, check: bool = True) -> str:
    return run(["git", *args], cwd=clone, check=check)


def gh(*args: str) -> str:
    return run(["gh", *args])


def short(sha: str | None) -> str:
    return sha[:12] if sha else "-"


@contextmanager
def cloned(remote: str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="ddak-demo-cycle-") as tmp:
        clone = Path(tmp) / "app"
        # "--" 뒤에 두어 '-'로 시작하는 --remote 값이 git 옵션으로 해석되지 않게 한다.
        run(["git", "clone", "--quiet", "--", remote, str(clone)])
        if git(clone, "status", "--porcelain", "--untracked-files=all"):
            raise DemoError("임시 clone 작업 트리가 깨끗하지 않다. 줄 끝 설정 등을 확인한다.")
        yield clone


def resolve(clone: Path, spec: str, what: str) -> str:
    value = git(clone, "rev-parse", "--verify", "--quiet", spec, check=False)
    if not value:
        raise DemoError(f"{what}을(를) 찾을 수 없다: {spec}")
    return value


def snapshot(clone: Path) -> Snapshot:
    prod = f"refs/remotes/origin/{BASE_BRANCH}"
    tags = sorted({action.tag for action in ACTIONS.values()})
    return Snapshot(
        prod_sha=resolve(clone, f"{prod}^{{commit}}", f"{BASE_BRANCH} 브랜치"),
        prod_tree=resolve(clone, f"{prod}^{{tree}}", f"{BASE_BRANCH} 트리"),
        tag_commits={t: resolve(clone, f"refs/tags/{t}^{{commit}}", f"태그 {t}") for t in tags},
        tag_trees={t: resolve(clone, f"refs/tags/{t}^{{tree}}", f"태그 {t} 트리") for t in tags},
    )


def tree_label(snap: Snapshot, tree: str | None) -> str:
    if tree is None:
        return "확인 불가"
    for tag, tag_tree in snap.tag_trees.items():
        if tree == tag_tree:
            return tag
    return "v1·v2 아님"


def optional(clone: Path, spec: str) -> str | None:
    return git(clone, "rev-parse", "--verify", "--quiet", spec, check=False) or None


def open_demo_prs(repo: str, clone: Path) -> list[DemoPr]:
    raw = gh(
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--base",
        BASE_BRANCH,
        "--limit",
        "100",
        "--json",
        "number,url,headRefName,headRefOid",
    )
    try:
        items = json.loads(raw or "[]")
    except ValueError as exc:
        raise DemoError("gh pr list 응답을 해석할 수 없다") from exc
    prs = []
    for item in items:
        branch = str(item.get("headRefName", ""))
        if not branch.startswith(DEMO_PREFIX):
            continue
        oid = str(item.get("headRefOid", ""))
        prs.append(
            DemoPr(
                number=int(item["number"]),
                url=str(item.get("url", "")),
                branch=branch,
                tree=optional(clone, f"{oid}^{{tree}}") if oid else None,
                parent_tree=optional(clone, f"{oid}^1^{{tree}}") if oid else None,
            )
        )
    return prs


def pr_line(snap: Snapshot, pr: DemoPr) -> str:
    # GitHub merge 결과는 PR 부모 트리가 지금 prod 트리와 같을 때만 PR 트리와 같다.
    if pr.parent_tree is None:
        freshness = "기준 확인 불가"
    elif pr.parent_tree == snap.prod_tree:
        freshness = "현재 prod 기준(merge해도 됨)"
    else:
        freshness = "오래됨(prod가 바뀜, 닫고 다시 만든다)"
    return f"  #{pr.number} {pr.branch} 트리={tree_label(snap, pr.tree)} {freshness}\n    {pr.url}"


def print_status(repo: str, snap: Snapshot, prs: list[DemoPr]) -> None:
    label = tree_label(snap, snap.prod_tree)
    print(f"저장소: {repo}")
    tags = ", ".join(f"{t}={short(c)}" for t, c in sorted(snap.tag_commits.items()))
    print(f"태그: {tags}")
    print(f"{BASE_BRANCH} HEAD: {short(snap.prod_sha)} 트리={label}")
    if label == "v1":
        print("  다음: v1 배포 확인 뒤 prepare-v2로 시연용 v2 PR을 연다.")
    elif label == "v2":
        print("  다음: reset-v1으로 v1 되돌림 PR을 열고 merge한다.")
    else:
        print("  주의: prod 내용이 v1·v2 어느 태그와도 다르다. reset-v1으로 v1부터 맞춘다.")
    print("열린 demo PR:")
    if not prs:
        print("  (없음)")
    for pr in prs:
        print(pr_line(snap, pr))


def build_commit(clone: Path, snap: Snapshot, name: str, action: Action) -> str:
    tag = action.tag
    body = (
        f"태그 {tag}({short(snap.tag_commits[tag])})의 트리를 그대로 쓴다. "
        f"부모는 현재 {BASE_BRANCH}({short(snap.prod_sha)})다. 이력은 보존한다."
    )
    commit = git(
        clone,
        "commit-tree",
        snap.tag_trees[tag],
        "-p",
        snap.prod_sha,
        "-m",
        action.title,
        "-m",
        body,
        "-m",
        f"harness/scripts/demo_cycle.py {name}",
    )
    if resolve(clone, f"{commit}^{{tree}}", "새 커밋 트리") != snap.tag_trees[tag]:
        raise DemoError("새 커밋 트리가 태그 트리와 다르다")
    if resolve(clone, f"{commit}^1", "새 커밋 부모") != snap.prod_sha:
        raise DemoError(f"새 커밋 부모가 {BASE_BRANCH}가 아니다")
    return commit


def push_branch(clone: Path, commit: str, branch: str) -> None:
    if not branch.startswith(DEMO_PREFIX) or branch == BASE_BRANCH:
        raise DemoError(f"demo/ 브랜치만 push한다: {branch}")
    if git(clone, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"):
        raise DemoError(f"원격에 같은 브랜치가 이미 있다: {branch}")
    # 강제 표시(+)·--force 없이 새 브랜치 하나만 만든다. 태그는 보내지 않는다.
    git(clone, "push", "--quiet", "--no-follow-tags", "origin", f"{commit}:refs/heads/{branch}")


def pr_body(snap: Snapshot, name: str, action: Action) -> str:
    tag = action.tag
    return "\n".join(
        [
            "## 변경 내용",
            f"- {action.purpose}",
            f"- 태그 `{tag}`({short(snap.tag_commits[tag])})의 트리를 그대로 쓰는 커밋 1개다. "
            f"부모는 현재 `{BASE_BRANCH}`({short(snap.prod_sha)})이고 이력을 보존한다.",
            f"- `harness/scripts/demo_cycle.py {name}`로 만들었다. merge는 사람이 한다.",
            "",
            "## 확인한 것",
            f"- 커밋 트리 = 태그 `{tag}` 트리, 부모 = `{BASE_BRANCH}` HEAD",
            "- 관리 페이지에 승인 대기 run이 없을 때 merge한다.",
        ]
    )


def prepare(repo: str, remote: str, name: str, dry_run: bool) -> int:
    action = ACTIONS[name]
    prefix = "[dry-run] " if dry_run else ""
    with cloned(remote) as clone:
        snap = snapshot(clone)
        target_tree = snap.tag_trees[action.tag]
        print(f"{prefix}저장소: {repo}")
        print(
            f"{prefix}{BASE_BRANCH} HEAD {short(snap.prod_sha)} 트리="
            f"{tree_label(snap, snap.prod_tree)}, 목표 태그 {action.tag}"
            f"({short(snap.tag_commits[action.tag])})"
        )
        if snap.prod_tree == target_tree:
            print(f"{prefix}변경 없음: {BASE_BRANCH} 트리가 이미 {action.tag}와 같다.")
            return 0
        prs = open_demo_prs(repo, clone)
        same = [pr for pr in prs if pr.kind() == name]
        ready = [pr for pr in same if pr.tree == target_tree and pr.parent_tree == snap.prod_tree]
        if ready:
            print(f"{prefix}이미 준비된 PR이 있다(새로 만들지 않음): #{ready[0].number}")
            print(ready[0].url)
            return 0
        if same:
            stale = ", ".join(f"#{pr.number}" for pr in same)
            raise DemoError(
                f"오래된 {name} PR이 열려 있다({stale}). merge하지 말고 닫은 뒤 다시 실행한다: "
                f"gh pr close <번호> --repo {repo} --delete-branch"
            )
        for pr in prs:
            print(f"{prefix}참고: 다른 demo PR이 열려 있다.\n{pr_line(snap, pr)}")
        commit = build_commit(clone, snap, name, action)
        branch = action.branch_prefix + time.strftime("%Y%m%d-%H%M%S")
        print(
            f"{prefix}새 커밋 {short(commit)}: 트리={action.tag}, "
            f"부모={BASE_BRANCH} {short(snap.prod_sha)}"
        )
        if dry_run:
            print(f"[dry-run] push 예정: {branch} (새 브랜치, force 없음, 태그 없음)")
            print(f"[dry-run] PR 예정: {BASE_BRANCH} ← {branch} 「{action.title}」")
            print("[dry-run] 원격 변경 없음.")
            return 0
        push_branch(clone, commit, branch)
        print(f"push 완료: {branch}")
        out = gh(
            "pr",
            "create",
            "--repo",
            repo,
            "--base",
            BASE_BRANCH,
            "--head",
            branch,
            "--title",
            action.title,
            "--body",
            pr_body(snap, name, action),
        )
        url = out.splitlines()[-1] if out else ""
        print(f"PR: {url}")
        print("merge는 사람이 GitHub에서 한다. 관리 페이지에 승인 대기 run이 없을 때 merge한다.")
        return 0


def status(repo: str, remote: str) -> int:
    with cloned(remote) as clone:
        snap = snapshot(clone)
        print_status(repo, snap, open_demo_prs(repo, clone))
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="시연 반복용 앱 저장소 PR 준비(merge는 사람이 GitHub에서 한다)"
    )
    parser.add_argument("action", choices=("reset-v1", "prepare-v2", "status"))
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub 저장소 OWNER/NAME")
    parser.add_argument(
        "--remote", help="clone URL(기본 https://github.com/<repo>.git, 시험용 로컬 경로 가능)"
    )
    parser.add_argument("--dry-run", action="store_true", help="push·PR 없이 할 일만 출력")
    args = parser.parse_args(argv)
    if not REPO_PATTERN.match(args.repo):
        parser.error("--repo는 OWNER/NAME 형식이어야 한다")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    remote = args.remote or f"https://github.com/{args.repo}.git"
    try:
        if args.action == "status":
            return status(args.repo, remote)
        return prepare(args.repo, remote, args.action, args.dry_run)
    except FileNotFoundError as exc:
        print(
            f"실행 파일이 없다: {exc.filename} (git·gh 설치와 gh 로그인을 확인한다)",
            file=sys.stderr,
        )
        return 1
    except DemoError as exc:
        print(f"중단: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
