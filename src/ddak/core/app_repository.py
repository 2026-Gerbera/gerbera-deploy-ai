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

from ddak.core.contracts.errors import DdakToolError, ErrorCode


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
    ) -> None:
        self.secret_scan = secret_scan
        self.path = path.resolve(strict=True)
        self.allow_local = allow_local
        self.timeout_s = timeout_s
        self.timings: list[dict[str, Any]] = []

    def git(self, *args: str, ok: tuple[int, ...] = (0,)) -> str:
        return self.git_bytes(*args, ok=ok).decode("utf-8", errors="strict").strip()

    def git_bytes(self, *args: str, ok: tuple[int, ...] = (0,)) -> bytes:
        started = time.monotonic()
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        argv = [
            "git",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "protocol.file.allow=" + ("always" if self.allow_local else "never"),
            *args,
        ]
        process = subprocess.Popen(
            argv,
            cwd=self.path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
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
            return prepare_candidate(self, *args)
        finally:
            self.timings.append(
                {"operation": "prepare_candidate", "elapsed_s": time.monotonic() - started}
            )

    def validate_candidate(self, *args: Any) -> None:
        from ddak.core.candidate import validate_candidate

        validate_candidate(self, *args)

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
        result: dict[str, Any] = {"status": "SKIPPED", "tags": [], "main_updated": False}
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
