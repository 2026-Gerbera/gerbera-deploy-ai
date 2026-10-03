"""CLI와 관리 페이지가 함께 쓰는 시연 PR 트리·이력·push 규칙."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

BASE_BRANCH = "prod"
DEMO_PREFIX = "demo/"


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


def short(sha: str | None) -> str:
    return sha[:12] if sha else "-"


def resolve(git: Callable[..., str], spec: str, what: str) -> str:
    value = git("rev-parse", "--verify", "--quiet", spec, check=False)
    if not value:
        raise DemoError(f"{what}을(를) 찾을 수 없다: {spec}")
    return value


def snapshot(git: Callable[..., str]) -> Snapshot:
    prod = f"refs/remotes/origin/{BASE_BRANCH}"
    tags = sorted({action.tag for action in ACTIONS.values()})
    return Snapshot(
        prod_sha=resolve(git, f"{prod}^{{commit}}", f"{BASE_BRANCH} 브랜치"),
        prod_tree=resolve(git, f"{prod}^{{tree}}", f"{BASE_BRANCH} 트리"),
        tag_commits={t: resolve(git, f"refs/tags/{t}^{{commit}}", f"태그 {t}") for t in tags},
        tag_trees={t: resolve(git, f"refs/tags/{t}^{{tree}}", f"태그 {t} 트리") for t in tags},
    )


def tree_label(snap: Snapshot, tree: str | None) -> str:
    if tree is None:
        return "확인 불가"
    for tag, tag_tree in snap.tag_trees.items():
        if tree == tag_tree:
            return tag
    return "v1·v2 아님"


def build_commit(git: Callable[..., str], snap: Snapshot, name: str, action: Action) -> str:
    tag = action.tag
    body = (
        f"태그 {tag}({short(snap.tag_commits[tag])})의 트리를 그대로 쓴다. "
        f"부모는 현재 {BASE_BRANCH}({short(snap.prod_sha)})다. 이력은 보존한다."
    )
    commit = git(
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
    if resolve(git, f"{commit}^{{tree}}", "새 커밋 트리") != snap.tag_trees[tag]:
        raise DemoError("새 커밋 트리가 태그 트리와 다르다")
    if resolve(git, f"{commit}^1", "새 커밋 부모") != snap.prod_sha:
        raise DemoError(f"새 커밋 부모가 {BASE_BRANCH}가 아니다")
    return commit


def push_branch(git: Callable[..., str], commit: str, branch: str) -> None:
    if not branch.startswith(DEMO_PREFIX) or branch == BASE_BRANCH:
        raise DemoError(f"demo/ 브랜치만 push한다: {branch}")
    if git("ls-remote", "--heads", "origin", f"refs/heads/{branch}"):
        raise DemoError(f"원격에 같은 브랜치가 이미 있다: {branch}")
    # 강제 표시(+)·--force 없이 새 브랜치 하나만 만든다. 태그는 보내지 않는다.
    git("push", "--quiet", "--no-follow-tags", "origin", f"{commit}:refs/heads/{branch}")


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
