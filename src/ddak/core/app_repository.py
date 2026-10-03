"""앱 저장소의 배포 기록. 팀 저장소를 조작하지 않으며 명시적으로 연결한 저장소만 쓴다."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.git_credentials import (
    CredentialSource,
    helper_options,
    isolated_git_env,
)
from ddak.core.git_credentials import (
    credential_source as resolve_credential_source,
)


def git_sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise ValueError("완전한 Git 커밋 SHA가 필요하다")
    return value


class AppRepository:
    """운영자가 지정한 앱 전용 checkout. allow_local은 로컬 bare 저장소 검증용이다."""

    def __init__(
        self,
        path: Path,
        *,
        allow_local: bool = False,
        timeout_s: float = 30,
        secret_scan: Callable[[Path], None] | None = None,
        expected_url: str | None = None,
        author: tuple[str, str] | None = None,
        credentials: tuple[Path, str, str] | None = None,
        credential_source: CredentialSource | None = None,
    ) -> None:
        self.author = author
        self.credentials = credentials
        # 재연결에서만 모드를 다시 고른다. 파생 worktree도 같은 선택을 물려받는다.
        self._credential_source: CredentialSource = credential_source or (
            resolve_credential_source(*credentials) if credentials else "machine"
        )
        self.secret_scan = secret_scan
        self.expected_url = expected_url
        self.path = path.resolve(strict=True)
        self.allow_local = allow_local
        self.timeout_s = timeout_s
        self.timings: list[dict[str, Any]] = []

    @property
    def credential_source(self) -> CredentialSource:
        """이 연결이 모든 Git 명령에 사용하는 출처. 토큰 값은 공개하지 않는다."""
        return self._credential_source

    @classmethod
    def connect(
        cls,
        path: Path,
        repo_url: str,
        *,
        allow_local: bool = False,
        author: tuple[str, str] | None = None,
        credentials: tuple[Path, str, str] | None = None,
    ) -> AppRepository:
        """제품 전용 checkout만 만든다. 기존 checkout의 원격을 자동 변경하지 않는다."""
        parts = urlsplit(repo_url)
        if (
            parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or any(c.isspace() for c in repo_url)
            or not (
                (parts.scheme == "https" and parts.hostname)
                or (allow_local and parts.scheme == "file" and not parts.netloc)
            )
        ):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "앱 저장소 URL 형식 오류")
        path.mkdir(parents=True, exist_ok=True)
        repo = cls(
            path,
            allow_local=allow_local,
            expected_url=repo_url,
            author=author,
            credentials=credentials,
        )
        try:
            if not (path / ".git").exists():
                if any(path.iterdir()):
                    raise DdakToolError(
                        ErrorCode.PRECONDITION_FAILED, "앱 checkout 경로가 비어 있지 않다"
                    )
                # URL을 바꾸는 Git 전역 설정도 승인 URL과 비교한다. 네트워크 조회는 없다.
                if repo.git("ls-remote", "--get-url", repo_url) != repo_url:
                    raise DdakToolError(
                        ErrorCode.PRECONDITION_FAILED, "앱 저장소 URL 재작성은 허용하지 않는다"
                    )
                repo.git(
                    "clone",
                    "--no-checkout",
                    "--template=",
                    "--origin",
                    "origin",
                    "--",
                    repo_url,
                    ".",
                )
            repo.require_origin(repo_url)
        except DdakToolError as error:
            if error.code in {ErrorCode.ADAPTER_FAILED, ErrorCode.ADAPTER_TIMEOUT}:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED,
                    "설정 필요: 앱 저장소 Git 인증·권한·연결 상태를 확인하세요",
                ) from None
            raise
        return repo

    def require_origin(self, repo_url: str) -> None:
        if self.expected_url is not None and self.expected_url != repo_url:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "승인 저장소와 연결된 저장소가 다르다"
            )
        if self.credential_source == "managed" and self.git(
            "config",
            "--includes",
            "--name-only",
            "--get-regexp",
            r"^http\..*extraheader$",
            ok=(0, 1),
        ):
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "앱 checkout에 별도 HTTP 인증 헤더가 있다"
            )
        fetch = self.git("remote", "get-url", "--all", "origin").splitlines()
        push = self.git("remote", "get-url", "--push", "--all", "origin").splitlines()
        if fetch != [repo_url] or push != [repo_url]:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "origin의 실제 fetch/push 대상이 승인 URL과 다르다"
            )
        self.expected_url = repo_url

    def resolve_tag(self, ref: str) -> str:
        """annotated 태그 객체가 아닌 최종 커밋을 고정한다. lightweight도 지원한다."""
        return self._resolve_tag(ref, "origin")

    def _resolve_tag(self, ref: str, remote: str) -> str:
        if not ref.startswith("refs/tags/v"):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "v* 태그 ref가 필요하다")
        self.git("check-ref-format", ref)
        if self.expected_url is not None:
            self.require_origin(self.expected_url)
        refs = {}
        for line in self.git("ls-remote", remote, ref, ref + "^{}").splitlines():
            sha, name = line.split("\t", 1)
            refs[name] = git_sha(sha)
        resolved = refs.get(ref + "^{}") or refs.get(ref)
        if resolved is None:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, f"요청한 태그를 찾을 수 없다: {ref}")
        return resolved

    def git(self, *args: str, ok: tuple[int, ...] = (0,)) -> str:
        return self.git_bytes(*args, ok=ok).decode("utf-8", errors="strict").strip()

    def git_bytes(self, *args: str, ok: tuple[int, ...] = (0,)) -> bytes:
        if args and args[0] in {"fetch", "push"} and self.expected_url is not None:
            self.require_origin(self.expected_url)
        started = time.monotonic()
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        options: list[str] = []
        if self.author and args and args[0] in {"merge", "commit", "commit-tree"}:
            # 명령에만 사람 신원을 적용한다. 상속된 신원으로 덮어쓰지 않는다.
            env = {
                k: v for k, v in env.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))
            }
            options += [
                "-c",
                "user.name=" + self.author[0],
                "-c",
                "user.email=" + self.author[1],
                "-c",
                "user.useConfigOnly=true",
                "-c",
                "author.name=" + self.author[0],
                "-c",
                "author.email=" + self.author[1],
                "-c",
                "committer.name=" + self.author[0],
                "-c",
                "committer.email=" + self.author[1],
            ]
        source = self.credential_source
        if self.credentials:
            if args and args[0] != "config":
                options += [
                    part
                    for option in helper_options(*self.credentials, source=source)
                    for part in ("-c", option)
                ]
        elif source == "machine":
            options += ["-c", "credential.interactive=false"]
        env = isolated_git_env(env, source=source)
        argv = [
            "git",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "http.followRedirects=false",
            "-c",
            "protocol.file.allow=" + ("always" if self.allow_local else "never"),
            *options,
            *args,
        ]
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.path,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            raise DdakToolError(
                ErrorCode.ADAPTER_FAILED, "앱 Git 실행 실패; 원문 출력은 숨김"
            ) from None
        try:
            stdout, _ = process.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "앱 Git 명령 제한 시간 초과") from None
        finally:
            self.timings.append(
                {
                    "operation": args[0],
                    "elapsed_s": time.monotonic() - started,
                    "returncode": process.returncode,
                }
            )
        if process.returncode not in ok:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "앱 Git 명령 실패; 원문 출력은 숨김")
        return stdout

    def prepare_candidate(self, *args: Any) -> dict[str, Any]:
        from ddak.core.candidate import prepare_candidate

        started = time.monotonic()
        try:
            return {**prepare_candidate(self, *args), "git_auth_source": self.credential_source}
        finally:
            self.timings.append(
                {"operation": "prepare_candidate", "elapsed_s": time.monotonic() - started}
            )

    def preflight_source(self, source_sha: str, *, patch: bytes | None = None) -> dict[str, Any]:
        from ddak.core.candidate import preflight_source

        self.check_push_access()
        return {
            **preflight_source(self, source_sha, patch=patch),
            "git_auth_source": self.credential_source,
        }

    def check_push_access(self, branch: str = "prod") -> None:
        """ai-prod의 현재 SHA로 일반 push를 dry-run한다. 원격 ref는 변경하지 않는다."""
        target = "refs/heads/ai-prod"
        try:
            source_ref = "refs/heads/" + branch.removeprefix("refs/heads/")
            self.git("check-ref-format", source_ref)
            if self.expected_url is not None:
                self.require_origin(self.expected_url)
            refs = {}
            for line in self.git("ls-remote", "--refs", "origin", source_ref, target).splitlines():
                oid, ref = line.split("\t", 1)
                refs[ref] = git_sha(oid)
            if source_ref not in refs:
                raise ValueError("감시 브랜치가 없다")
            sha = refs.get(target, refs[source_ref])
            self.git("fetch", "--no-tags", "origin", sha)
            self.git("push", "--dry-run", "--porcelain", "origin", sha + ":" + target)
        except (DdakToolError, OSError, ValueError):
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED,
                "설정 필요: 앱 저장소 push 접근 확인 실패; Git 인증·권한·연결 상태를 확인하세요",
            ) from None

    def validate_candidate(self, *args: Any) -> dict[str, Any]:
        from ddak.core.candidate import validate_candidate

        return validate_candidate(self, *args)

    def publish(
        self,
        candidate_sha: str,
        selected: set[str],
        succeeded: set[str],
        *,
        update_main: bool = True,
    ) -> dict[str, Any]:
        """성공 환경 태그부터 기록한다. main은 전체 선택 성공 때만 fast-forward 한다."""
        started = time.monotonic()
        first_timing = len(self.timings)
        candidate_sha = git_sha(candidate_sha)
        if not selected or not selected <= {"local", "cloud"} or not succeeded <= selected:
            raise ValueError("배포 기록 대상이 잘못됐다")
        result: dict[str, Any] = {
            "status": "SKIPPED",
            "tags": [],
            "main_updated": False,
            "git_auth_source": self.credential_source,
        }
        if not succeeded:
            return result
        try:
            self.git("cat-file", "-e", candidate_sha + "^{commit}")
            self.git(
                "fetch", "--no-tags", "origin", "refs/heads/ai-prod:refs/remotes/origin/ai-prod"
            )
            self.git("merge-base", "--is-ancestor", candidate_sha, "refs/remotes/origin/ai-prod")
            refs = {}
            for line in self.git(
                "ls-remote", "--refs", "origin", "refs/tags/deployed/*", "refs/heads/main"
            ).splitlines():
                sha, name = line.split("\t", 1)
                refs[name] = git_sha(sha)
            tags = [
                "refs/tags/deployed/" + ("onprem" if t == "local" else "cloud")
                for t in sorted(succeeded)
            ]
            # 이동하는 배포 태그에만 명시적 old-OID lease를 사용한다. 브랜치 force는 없다.
            self.git(
                "push",
                "--atomic",
                *("--force-with-lease=" + tag + ":" + refs.get(tag, "") for tag in tags),
                "origin",
                *(candidate_sha + ":" + tag for tag in tags),
            )
            result.update(status="SUCCEEDED", tags=tags)
            if not update_main:
                result["main_skip_reason"] = "verification_failed"
            if succeeded == selected and update_main:
                if "refs/heads/main" in refs:
                    self.git(
                        "fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main"
                    )
                    self.git(
                        "merge-base", "--is-ancestor", "refs/remotes/origin/main", candidate_sha
                    )
                self.git("push", "origin", candidate_sha + ":refs/heads/main")
                result["main_updated"] = True
        except DdakToolError as exc:
            result.update(status="PARTIAL" if result["tags"] else "FAILED", error=exc.code.value)
        finally:
            result["elapsed_s"] = time.monotonic() - started
            result["commands"] = self.timings[first_timing:]
        return result


class FakeAppRepository(AppRepository):
    """UI 리허설용 가짜 후보·게시. 원격 읽기는 허용하되 Git 쓰기는 하지 않는다."""

    def require_origin(self, repo_url: str) -> None:
        if repo_url != self.expected_url:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "가짜 저장소 승인 URL 불일치")

    def git_bytes(self, *args: str, ok: tuple[int, ...] = (0,)) -> bytes:
        if not args or args[0] not in {"ls-remote", "check-ref-format"}:
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "가짜 저장소는 Git 쓰기를 실행하지 않는다"
            )
        return super().git_bytes(*args, ok=ok)

    def resolve_tag(self, ref: str) -> str:
        if self.expected_url is None:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "가짜 저장소 URL이 필요하다")
        return self._resolve_tag(ref, self.expected_url)

    def prepare_candidate(self, *args: Any) -> dict[str, Any]:
        from ddak.core.snapshots import digest_json, file_manifest

        _sha, _files, build_files, _patch, _work, guard, source = args
        guard()
        if file_manifest(source) != build_files:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "가짜 후보도 승인 트리와 같아야 한다"
            )
        return {
            "candidate_sha": digest_json(build_files).split(":", 1)[1][:40],
            "merge_conflicts": [],
            "source": "fake",
            "git_auth_source": self.credential_source,
        }

    def validate_candidate(self, *args: Any) -> dict[str, Any]:
        from ddak.core.snapshots import digest_json

        _sha, candidate_sha, _files, build_files, _work, guard = args
        guard()
        if candidate_sha != digest_json(build_files).split(":", 1)[1][:40]:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "가짜 후보 해시 불일치")
        return {"source": "fixture", "ignored_count": 0, "findings": []}

    def publish(self, candidate_sha, selected, succeeded, *, update_main=True):
        return {
            "status": "SIMULATED",
            "source": "fake",
            "git_auth_source": self.credential_source,
            "tags": [],
            "main_updated": False,
            "would_publish": sorted(succeeded),
            "would_update_main": update_main and succeeded == selected,
        }
