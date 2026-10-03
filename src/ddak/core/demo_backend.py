"""시연 PR의 Git/GitHub 어댑터. 쓰기는 새 demo/ 브랜치와 PR 생성뿐이다."""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ddak.core.app_repository import AppRepository, git_sha
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.demo_cycle import (
    ACTIONS,
    DemoError,
    build_commit,
    pr_body,
    push_branch,
    snapshot,
    tree_label,
)
from ddak.core.git_credentials import configured_identity, isolated_git_env, require_token


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_json(url: str, *, headers=None, data=None) -> dict | list:
    if urlsplit(url).scheme not in ("https", "http"):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "HTTP 서비스 주소가 필요합니다")
    request = Request(url, headers=headers or {}, data=data)  # noqa: S310 - HTTP(S)만 허용
    try:
        with build_opener(NoRedirect()).open(request, timeout=8) as response:
            payload = response.read(256 * 1024 + 1)
            if len(payload) > 256 * 1024:
                raise ValueError
            return json.loads(payload)
    except (OSError, ValueError, HTTPError, URLError):
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED, "연결 확인 실패: 주소·인증·접근 권한을 확인하세요"
        ) from None


def repository_name(url: str) -> str:
    parsed = urlsplit(url)
    name = parsed.path.strip("/").removesuffix(".git")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name)
    ):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "GitHub 앱 저장소 URL을 확인하세요")
    return name


def api_token(repository, credentials: tuple[Path, str, str]) -> str:
    """관리 토큰 우선, 없을 때만 같은 URL의 머신 Git helper를 조회한다."""
    if repository.credential_source == "managed":
        return require_token(*credentials)
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "credential.interactive=false",
                "-c",
                "credential.useHttpPath=true",
                "credential",
                "fill",
            ],
            cwd=repository.path,
            input="url=" + credentials[2] + "\n\n",
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
            env=isolated_git_env(dict(os.environ), source="machine"),
        )
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        token = fields.get("password", "")
        if result.returncode == 0 and token and not any(c.isspace() for c in token):
            return token
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise DdakToolError(
        ErrorCode.PRECONDITION_FAILED,
        "설정 필요: 앱 저장소 push 토큰(PR 생성 권한 포함) 또는 머신 Git 자격 증명",
    )


class Github:
    def __init__(self, name: str, token: str):
        self.name, self._token = name, token

    def request(self, path, body=None):
        return read_json(
            f"https://api.github.com/repos/{self.name}/{path}",
            headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
            data=json.dumps(body).encode() if body is not None else None,
        )

    def open_prs(self):
        return self.request(
            "pulls?" + urlencode({"state": "open", "base": "prod", "per_page": 100})
        )

    def create(self, branch, title, body):
        return self.request("pulls", {"head": branch, "base": "prod", "title": title, "body": body})

    def get(self, number) -> dict[str, Any]:
        data = self.request(f"pulls/{int(number)}")
        if not isinstance(data, dict):
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "GitHub PR 응답 형식 오류")
        return data


class DemoBackend:
    def __init__(self, root: Path, *, connect=AppRepository.connect, github=None, enabled=True):
        self.root, self.connect, self.enabled = root, connect, enabled
        self.github = github or (
            lambda repo, cred: Github(repository_name(cred[2]), api_token(repo, cred))
        )

    def _open(self, project, saved):
        if not self.enabled:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED,
                "FAKE 모드에서는 실제 시연 PR을 만들거나 조회하지 않습니다",
            )
        url = saved.get("repo_url") or ""
        repository_name(url)
        credentials = (self.root / "private", project, url)
        repo = self.connect(
            self.root / "demo-reset" / "repositories" / project,
            url,
            author=configured_identity(saved),
            credentials=credentials,
        )
        repo.git(
            "fetch",
            "--no-tags",
            "origin",
            "refs/heads/prod:refs/remotes/origin/prod",
            "refs/tags/v1:refs/tags/v1",
            "refs/tags/v2:refs/tags/v2",
        )

        def git(*args, check=True):
            return repo.git(*args, ok=(0,) if check else (0, 1, 128))

        return repo, git, self.github(repo, credentials)

    def create(self, project, saved, action_name):
        repo, git, github = self._open(project, saved)
        snap = snapshot(git)
        action = ACTIONS[action_name]
        target = snap.tag_trees[action.tag]
        base = {"tag": action.tag, "target_tree": target, "repo_url": saved["repo_url"]}
        if target == snap.prod_tree:
            return {**base, "already_source": True, "merge_sha": snap.prod_sha}
        for pr in github.open_prs():
            branch = pr["head"]["ref"]
            if not branch.startswith(action.branch_prefix):
                continue
            head = git_sha(pr["head"]["sha"])
            repo.git("fetch", "--no-tags", "origin", f"refs/heads/{branch}")
            if (
                git("rev-parse", head + "^{tree}") != target
                or git("rev-parse", head + "^1^{tree}") != snap.prod_tree
            ):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED,
                    f"기준이 바뀐 시연 PR #{pr['number']}이 열려 있습니다. "
                    "GitHub에서 닫고 다시 준비하세요",
                )
            return {**base, **self._public_pr(pr, saved), "branch": branch}
        try:
            commit = build_commit(git, snap, action_name, action)
            branch = action.branch_prefix + uuid.uuid4().hex[:12]
            push_branch(git, commit, branch)
        except DemoError as error:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, str(error)) from None
        pr = github.create(branch, action.title, pr_body(snap, action_name, action))
        return {**base, **self._public_pr(pr, saved), "branch": branch}

    @staticmethod
    def _public_pr(pr, saved):
        number = int(pr["number"])
        return {
            "number": number,
            "url": f"https://github.com/{repository_name(saved['repo_url'])}/pull/{number}",
        }

    def inspect(self, project, saved, records, sources):
        _, git, github = self._open(project, saved)
        snap = snapshot(git)
        prs = {}
        for name, record in records.items():
            if record.get("number"):
                pr = github.get(record["number"])
                prs[name] = {
                    "merged": bool(pr.get("merged")),
                    "closed": pr.get("state") == "closed",
                    "merge_sha": git_sha(pr["merge_commit_sha"]) if pr.get("merged") else None,
                }
            else:
                prs[name] = {"merged": True, "merge_sha": record.get("merge_sha")}
        labels = {}
        for source in sources:
            if source:
                tree = git("rev-parse", "--verify", source + "^{tree}", check=False)
                labels[source] = tree_label(snap, tree or None)
        return {"prs": prs, "labels": labels}


def probe_version(url: str) -> dict:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "공개 서비스 주소 형식 오류")
    data = read_json(url.rstrip("/") + "/version")
    if not isinstance(data, dict):
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "/version 응답 형식 오류")
    result = {}
    for key in ("release_id", "source_sha", "version"):
        value = data.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,160}", value):
            result[key] = value
    return result
