"""승인된 prod 원본을 ai-prod 이력에 합치고 같은 수정본만 일반 push 한다."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from ddak.core.app_repository import AppRepository, git_sha
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.snapshots import apply_diff, digest_bytes, excluded, file_manifest


class CandidateConflict(DdakToolError):
    def __init__(self, source_sha: str, files: list[str]) -> None:
        super().__init__(ErrorCode.PRECONDITION_FAILED, "후보 충돌: 충돌 파일 재패치 필요")
        self.proposal = {
            "status": "NEEDS_REPATCH",
            "source_sha": source_sha,
            "files": files,
            "reason": "prod_merge_conflict",
        }


def template_file(path: Path) -> bool:
    return path.name in {".env.example", ".env.sample"} and not excluded(path.parent)


def tree_manifest(
    repository: AppRepository, revision: str, *, templates: bool = False
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in repository.git_bytes("ls-tree", "-rz", "--full-tree", revision).split(b"\0"):
        if not item:
            continue
        metadata, raw_name = item.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        name = raw_name.decode("utf-8")
        path = PurePosixPath(name)
        if (
            kind != "blob"
            or mode not in ("100644", "100755")
            or path.is_absolute()
            or ".." in path.parts
            or (excluded(Path(name)) and not template_file(Path(name)))
        ):
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "후보 Git 트리에 지원하지 않는 파일이 있다"
            )
        if template_file(Path(name)) != templates:
            continue
        result[name] = {
            "sha256": digest_bytes(repository.git_bytes("cat-file", "blob", oid)),
            "executable": mode == "100755",
        }
    return result


def scan_staged(repository: Path) -> None:
    """trusted 설정과 빈 ignore 목록으로 커밋 직전 검사. 도구가 없으면 실패한다."""
    with tempfile.TemporaryDirectory(prefix="ddak-secret-scan-") as root:
        config = Path(root) / "gitleaks.toml"
        config.write_text("[extend]\nuseDefault = true\n")
        ignore = Path(root) / "empty.ignore"
        ignore.write_text("")
        try:
            result = subprocess.run(
                [
                    "gitleaks",
                    "dir",
                    "--redact",
                    "--no-banner",
                    "--ignore-gitleaks-allow",
                    "--gitleaks-ignore-path",
                    str(ignore),
                    "--config",
                    str(config),
                    str(repository),
                ],
                cwd=root,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "비밀값 검사 도구 실행 실패; 커밋하지 않음"
            ) from None
        if result.returncode:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "비밀값 검사 실패; 커밋하지 않음")


def _validate_trees(
    repository: AppRepository,
    source_sha: str,
    candidate_sha: str,
    source_files: dict[str, Any],
    build_files: dict[str, Any],
) -> None:
    if tree_manifest(repository, git_sha(source_sha)) != source_files:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "prod SHA와 승인 원본이 다르다")
    if tree_manifest(repository, git_sha(candidate_sha)) != build_files:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "후보 SHA와 승인 수정본이 다르다")
    if tree_manifest(repository, source_sha, templates=True) != tree_manifest(
        repository, candidate_sha, templates=True
    ):
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "템플릿은 prod 원본을 유지해야 한다")
    repository.git("merge-base", "--is-ancestor", source_sha, candidate_sha)


def validate_candidate(
    repository: AppRepository,
    source_sha: str,
    candidate_sha: str,
    source_files: dict[str, Any],
    build_files: dict[str, Any],
    workspace: Path,
    guard: Callable[[], None],
) -> None:
    for branch in ("prod", "ai-prod"):
        repository.git(
            "fetch", "--no-tags", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}"
        )
    repository.git("merge-base", "--is-ancestor", git_sha(source_sha), "refs/remotes/origin/prod")
    repository.git(
        "merge-base", "--is-ancestor", git_sha(candidate_sha), "refs/remotes/origin/ai-prod"
    )
    _validate_trees(repository, source_sha, candidate_sha, source_files, build_files)
    guard()
    repository.git("worktree", "add", "--detach", str(workspace), candidate_sha)
    if file_manifest(workspace) != build_files:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "후보 검사 사본이 승인 트리와 다르다")
    (repository.secret_scan or scan_staged)(workspace)
    if file_manifest(workspace) != build_files:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "검사 중 후보 사본이 바뀌었다")
    guard()


def prepare_candidate(
    repository: AppRepository,
    source_sha: str,
    source_files: dict[str, Any],
    build_files: dict[str, Any],
    patch: bytes | None,
    workspace: Path,
    guard: Callable[[], None],
    approved_source: Path | None = None,
) -> dict[str, Any]:
    source_sha = git_sha(source_sha)
    repository.git("fetch", "--no-tags", "origin", "refs/heads/prod:refs/remotes/origin/prod")
    repository.git("merge-base", "--is-ancestor", source_sha, "refs/remotes/origin/prod")
    if tree_manifest(repository, source_sha) != source_files:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "prod SHA와 승인 원본이 다르다")
    remote_ai = repository.git("ls-remote", "--refs", "origin", "refs/heads/ai-prod")
    starting = source_sha
    if remote_ai:
        repository.git(
            "fetch", "--no-tags", "origin", "refs/heads/ai-prod:refs/remotes/origin/ai-prod"
        )
        starting = git_sha(repository.git("rev-parse", "refs/remotes/origin/ai-prod"))
    tree_manifest(repository, starting)  # 제외 파일/링크는 checkout 이전에도 거부한다.
    guard()
    repository.git("worktree", "add", "--detach", str(workspace), starting)
    work = AppRepository(
        workspace,
        allow_local=repository.allow_local,
        timeout_s=repository.timeout_s,
        secret_scan=repository.secret_scan,
    )
    scanner = repository.secret_scan or scan_staged
    audit = workspace.parent / "candidate-attempt.json"

    conflicts: list[str] = []

    def record(phase: str, sha: str) -> None:
        audit.write_text(
            json.dumps(
                {
                    "phase": phase,
                    "commit_sha": sha,
                    "source_sha": source_sha,
                    "merge_conflicts": conflicts,
                }
            )
        )

    record("CHECKED_OUT", starting)
    try:
        work.git("merge", "--no-ff", "--no-commit", source_sha)
    except DdakToolError:
        conflicts = [
            n for n in work.git("diff", "--name-only", "--diff-filter=U", "-z").split("\0") if n
        ]
        if not conflicts:
            raise
    record("MERGED", starting)

    def write_files(destination: Path, revision: str, files: dict[str, Any]) -> None:
        for name, metadata in files.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(work.git_bytes("cat-file", "blob", revision + ":" + name))
            target.chmod(0o755 if metadata["executable"] else 0o644)

    with tempfile.TemporaryDirectory(prefix="ddak-approved-tree-") as directory:
        approved = approved_source or Path(directory)
        if approved_source is None:
            write_files(approved, source_sha, source_files)
            if patch:
                apply_diff(approved, patch)
        if file_manifest(approved) != build_files:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "승인 수정본 사본과 해시가 다르다")
        # 현재 후보의 관리 파일을 전부 비우고 승인한 내용만 넣는다. .git은 제외된다.
        for name in file_manifest(workspace):
            (workspace / name).unlink()
        # prod 템플릿은 관리 대상 밖이지만 이전 ai-prod/충돌 내용은 남기지 않는다.
        for path in workspace.rglob(".env.*"):
            if template_file(path.relative_to(workspace)) and path.is_file():
                path.unlink()
        for path in sorted(workspace.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if (
                path.is_dir()
                and not excluded(path.relative_to(workspace))
                and not any(path.iterdir())
            ):
                path.rmdir()
        for name, metadata in build_files.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(approved / name, target)
            target.chmod(0o755 if metadata["executable"] else 0o644)
        write_files(workspace, source_sha, tree_manifest(work, source_sha, templates=True))
        if file_manifest(approved) != build_files or file_manifest(workspace) != build_files:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "복사 중 승인 수정본이 바뀌었다")

    work.git("add", "--all", "--", ".")
    approved_paths = [*build_files, *tree_manifest(work, source_sha, templates=True)]
    if approved_paths:
        # 승인한 일반 파일/템플릿만 강제로 staging한다. 앱 ignore 규칙에 맡기지 않는다.
        work.git("add", "--force", "--", *approved_paths)
    tree = work.git("write-tree")
    if tree_manifest(work, tree) != build_files:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "승인 수정본과 staged 트리가 다르다")
    scanner(workspace)  # commit-tree에도 커밋 직전 검사를 명시적으로 적용한다.
    if work.git("write-tree") != tree or file_manifest(workspace) != build_files:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "검사 중 staged 트리가 변경됐다")
    guard()
    parents = ["-p", starting]
    if source_sha != starting:  # 첫 후보는 같은 부모를 중복 기록하지 않는다.
        parents += ["-p", source_sha]
    candidate_sha = git_sha(
        work.git("commit-tree", tree, *parents, "-m", "Merge approved deployment tree")
    )
    record("LOCAL_COMMIT", candidate_sha)
    _validate_trees(work, source_sha, candidate_sha, source_files, build_files)
    # 재사용 후보도 검사하며 모든 push 직전 승인을 다시 확인한다.
    scanner(workspace)
    if file_manifest(workspace) != build_files:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "검사 중 후보 사본이 바뀌었다")
    guard()
    record("READY_TO_PUSH", candidate_sha)
    work.git("push", "origin", candidate_sha + ":refs/heads/ai-prod")
    record("PUSHED", candidate_sha)
    repository.timings.extend(work.timings)
    return {
        "candidate_sha": candidate_sha,
        "source_sha": source_sha,
        "workspace": str(workspace),
        "reused": candidate_sha == starting,
        "merge_conflicts": conflicts,
    }
