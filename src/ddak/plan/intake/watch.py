"""레포 감시: main을 10초마다 ls-remote로 확인하고 새 커밋이면 핸들러(계획 생성·검증 시작)를 부른다.

배포 실행은 여기서 시작하지 않는다(사람 승인 뒤). AI 없음. 마지막 처리 커밋은 파일에 저장한다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.logging import get_logger
from ddak.plan.intake.fetch import resolve_head, warm_cache
from ddak.plan.intake.policy import FetchPolicy

__all__ = ["WatchTarget", "Watcher", "load_watch_targets"]

_log = get_logger("plan")
PLACEHOLDER_URL = "https://github.com/<owner>/<repo>"
_SHA = re.compile(r"[0-9a-f]{40}")
_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_BACKOFF_S = 60.0
HANDLER_RETRIES = 3


@dataclass(frozen=True)
class WatchTarget:
    """나중에 UI가 저장할 값. 지금은 하드코딩."""

    project: str
    repo_url: str
    ref: str = "prod"
    target: Literal["local", "cloud", "both"] = "local"


HARDCODED_TARGETS = (WatchTarget(project="flaskr", repo_url=PLACEHOLDER_URL),)


def load_watch_targets(environ: Mapping[str, str] | None = None) -> list[WatchTarget]:
    """교체 지점: 나중에 프로젝트 설정(Store)에서 읽도록 이 함수 본문만 바꾼다."""
    env = os.environ if environ is None else environ
    url = env.get("DDAK_WATCH_REPO_URL") or None
    project = env.get("DDAK_WATCH_PROJECT") or "flaskr"
    selected = env.get("DDAK_WATCH_TARGETS") or "local"
    selected = "local" if selected == "onprem" else selected
    if selected not in {"local", "cloud", "both"} or not _PROJECT.fullmatch(project):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "감시 프로젝트/대상 형식 오류")
    targets = [
        WatchTarget(
            project, url or PLACEHOLDER_URL, env.get("DDAK_WATCH_BRANCH") or "prod", selected
        )
    ]
    if any(t.repo_url == PLACEHOLDER_URL for t in targets):
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "감시할 저장소가 정해지지 않았다. DDAK_WATCH_REPO_URL을 지정한다",
        )
    return targets


def interval_from_env(environ: Mapping[str, str] | None = None) -> float:
    env = os.environ if environ is None else environ
    raw = env.get("DDAK_WATCH_INTERVAL_S") or "10"
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "DDAK_WATCH_INTERVAL_S는 양수여야 한다")
    return value


# ---- 상태 파일 --------------------------------------------------------------------------------


def _state_path(policy: FetchPolicy, t: WatchTarget) -> Path:
    if not _PROJECT.fullmatch(t.project):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "project 이름이 올바르지 않다")
    return policy.root / "watch" / f"{t.project}.json"


def _url_hash(t: WatchTarget) -> str:
    return hashlib.sha256(t.repo_url.encode()).hexdigest()


def read_last_commit(policy: FetchPolicy, t: WatchTarget) -> str | None:
    """없음·손상·다른 저장소/ref는 None(기준선 없음). 예외로 멈추지 않는다."""
    path = _state_path(policy, t)
    try:
        data = json.loads(path.read_text())
        if (
            data["repo_url_hash"] == _url_hash(t)
            and data["ref"] == t.ref
            and _SHA.fullmatch(data["last_commit"])
        ):
            return data["last_commit"]
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        pass
    _log.warning("감시 상태 파일을 쓸 수 없어 기준선을 다시 잡는다", project=t.project)
    return None


def write_last_commit(
    policy: FetchPolicy, t: WatchTarget, sha: str, now: Callable[[], float] = time.time
) -> None:
    path = _state_path(policy, t)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    body = {
        "repo_url_hash": _url_hash(t),
        "ref": t.ref,
        "last_commit": sha,
        "updated_at": now(),
    }
    tmp.write_text(json.dumps(body))
    tmp.replace(path)  # 원자적 교체


# ---- 루프 -------------------------------------------------------------------------------------

Handler = Callable[[WatchTarget, str], Awaitable[None]]
Resolve = Callable[[str, str | None, FetchPolicy], str]
Warm = Callable[[WatchTarget, FetchPolicy], str]


def _default_resolve(url: str, ref: str | None, policy: FetchPolicy) -> str:
    return resolve_head(url, ref, policy=policy)


def _default_warm(t: WatchTarget, policy: FetchPolicy) -> str:
    return warm_cache(t.repo_url, t.ref, project=t.project, policy=policy)


class Watcher:
    def __init__(
        self,
        targets: list[WatchTarget],
        on_new_commit: Handler,
        *,
        policy: FetchPolicy,
        interval_s: float = 10.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        resolve: Resolve = _default_resolve,
        warm: Warm = _default_warm,
    ):
        self.targets, self.on_new_commit, self.policy = targets, on_new_commit, policy
        self.interval_s, self.clock, self._sleep = interval_s, clock, sleep
        self.resolve, self.warm = resolve, warm
        self._stop = asyncio.Event()

    async def run(self) -> None:
        await asyncio.gather(*(self._watch(t) for t in self.targets))

    async def stop(self) -> None:
        self._stop.set()

    async def _nap(self, seconds: float) -> None:
        """sleep하되 stop()이 오면 즉시 깨어난다."""
        nap = asyncio.ensure_future(self._sleep(seconds))
        stopped = asyncio.ensure_future(self._stop.wait())
        _, pending = await asyncio.wait({nap, stopped}, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()

    async def _watch(self, t: WatchTarget) -> None:
        last = read_last_commit(self.policy, t)
        pending: str | None = None
        handler: asyncio.Task[str] | None = None  # 처리 중인 SHA를 결과로 돌려준다
        fails = 0
        try:
            while not self._stop.is_set():
                delay = self.interval_s
                try:
                    head = await asyncio.to_thread(self.resolve, t.repo_url, t.ref, self.policy)
                    fails = 0
                    if last is None:  # 기준선: 기록만 하고 트리거하지 않는다
                        last = head
                        write_last_commit(self.policy, t, head, self.clock)
                        await self._warm(t)
                    elif head != last and head != (pending or ""):
                        pending = head  # 합치기: 이전 대기는 버리고 최신 하나만
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # 폴링 오류가 루프를 죽이면 안 된다
                    fails += 1
                    delay = min(self.interval_s * 2**fails, MAX_BACKOFF_S)
                    _log.warning("감시 폴링 실패", project=t.project, error=str(e)[:200])
                if handler is not None and handler.done():
                    last = handler.result()
                    handler = None
                if handler is None and pending is not None and pending != last:
                    handler = asyncio.create_task(self._handle(t, pending))
                    pending = None
                await self._nap(delay)
        finally:
            if handler is not None:
                handler.cancel()

    async def _warm(self, t: WatchTarget) -> None:
        try:
            await asyncio.to_thread(self.warm, t, self.policy)
        except Exception as e:  # 캐시 예열 실패는 감시를 막지 않는다
            _log.warning("캐시 예열 실패", project=t.project, error=str(e)[:200])

    async def _handle(self, t: WatchTarget, sha: str) -> str:
        """핸들러를 최대 3회 시도. 성공이든 포기든 처리 완료로 저장하고 SHA를 돌려준다."""
        for attempt in range(1, HANDLER_RETRIES + 1):
            try:
                await self.on_new_commit(t, sha)
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _log.error(
                    "새 커밋 처리 실패", project=t.project, attempt=attempt, error=str(e)[:200]
                )
                if attempt < HANDLER_RETRIES:
                    await self._nap(self.interval_s)
                    if self._stop.is_set():
                        raise asyncio.CancelledError from None
        try:
            write_last_commit(self.policy, t, sha, self.clock)
        except OSError as e:
            _log.warning("감시 상태 저장 실패", project=t.project, error=str(e)[:200])
        return sha
